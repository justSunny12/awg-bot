"""
handlers/common.py — общие помощники для роутеров.

Здесь: отправка конфига (ссылка/файл), безопасное редактирование сообщений,
и обёртка вызова синхронных services через to_thread.
"""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest

from aiogram.types import BufferedInputFile, CallbackQuery, LinkPreviewOptions, Message

# Меню и нав-экраны несут deep-link'и на самого бота (t.me/…): Telegram рисует
# им превью-вложение, которое в меню не нужно никогда.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

from awgbot.bot import keyboards as kb
from awgbot.bot.notifier import notify_one


async def call(fn, *args, **kwargs):
    """Синхронный service-вызов вне event loop (docker exec/БД не морозят loop)."""
    return await asyncio.to_thread(fn, *args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Reply-слой: «Меню» (обычное состояние) / «Отмена» (во время текст-ввода).
# Reply-клавиатуру в Telegram нельзя снять «в моменте» — только попутно с
# отправкой сообщения. Поэтому её состояние выставляется на исходящих
# сообщениях в точках-переходах: вход в ввод → Отмена, показ меню → снять.
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Единственное активное inline-меню в чате. Храним id последнего нав-сообщения
# в ui_state; при показе нового — гасим кнопки у прежнего. Так в чате всегда
# ровно одно живое меню, а старые кнопки нельзя нажать (линейность не рушится).
# ─────────────────────────────────────────────────────────────────────────────

async def _dismiss_previous_nav(bot, services, chat_id: int, keep_id=None) -> None:
    """Снять inline-кнопки у ранее показанного нав-сообщения (если оно не то же,
    что сейчас редактируем). Ошибки (сообщение удалено/старое) — глушим."""
    prev = await call(services.db.get_nav_message_id, chat_id)
    if prev is None or prev == keep_id:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=prev,
                                            reply_markup=None)
    except Exception:
        pass


async def drop_previous_nav(bot, services, chat_id: int) -> None:
    """Удалить прежнее нав-сообщение целиком, а не снять с него кнопки: когда
    следующий экран приходит новым сообщением по внешнему событию (файл
    конфигурации в чате агента), панель без кнопок над ним — просто мусор."""
    prev = await call(services.db.get_nav_message_id, chat_id)
    if prev is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=prev)
    except Exception:                                      # noqa: BLE001
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=prev, reply_markup=None)
        except Exception:                                  # noqa: BLE001
            pass
    await call(services.db.set_nav_message_id, chat_id, None)


async def send_menu(message: Message, services, text, markup, keep_id=None) -> None:
    """Показать меню/нав-экран НОВЫМ сообщением, погасив предыдущее активное.
    Единая точка показа — держит инвариант «одно живое меню в чате».
    keep_id — активное сообщение, у которого кнопки уже сняты вызывающим
    (итог операции): гасить его повторно — гарантированный «not modified»."""
    chat_id = message.chat.id
    await _dismiss_previous_nav(message.bot, services, chat_id, keep_id=keep_id)
    sent = await message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    await call(services.db.nav_touch, chat_id, sent.message_id)


async def delete_many(bot, chat_id: int, ids: list) -> None:
    """Удалить пачку сообщений одним запросом (deleteMessages, до 100 id);
    батч роняет один недоступный id — тогда поштучно. /start у админа удалял
    до 30 старых меню по одному round-trip — до 10 секунд."""
    ids = [int(i) for i in ids if i]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id=chat_id, message_ids=chunk)
            continue
        except Exception:                                  # noqa: BLE001
            pass
        for mid in chunk:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:                              # noqa: BLE001
                pass


async def purge_menus(bot, services, chat_id: int) -> None:
    """Убрать ВСЕ прошлые меню чата — для повторного /start.

    /start — это «начать заново», и оставлять над новой панелью стопку прежних
    (пусть и без кнопок) значит заставлять листать историю ради одного живого
    экрана. Удаляем всё, что помним; что Telegram удалить не даст (старше 48 ч),
    молча пропускаем — оно и так мёртвое.
    """
    ids = await call(services.db.pop_nav_history, chat_id)
    await delete_many(bot, chat_id, ids)
    await call(services.db.set_nav_message_id, chat_id, None)


async def dismiss_update_reports(bot, services, keep=None) -> None:
    """Снять кнопку «В меню» у всех прошлых окон обновления (тексты остаются —
    история «какая ступень чем закончилась» ценна). keep — (chat, msg), которое
    трогать не надо: оно и есть текущее."""
    for chat_id, mid in await call(services.pop_update_reports):
        if keep and (chat_id, mid) == tuple(keep):
            continue
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid,
                                                reply_markup=None)
        except Exception:                                  # noqa: BLE001
            pass


async def _track_content(services, sent) -> None:
    """Запомнить id контент-сообщения (ссылка/QR/файл) для удаления при возврате."""
    if services is None or sent is None:
        return
    await call(services.db.add_content_msg_id, sent.chat.id, sent.message_id)


async def ask_here(cb: CallbackQuery, services, state, prompt: str, kind: str,
                   ref: int = 0, **data) -> None:
    """Приглашение к вводу НА МЕСТЕ экрана, с инлайн «✖️ Отмена». В FSM-данные
    кладётся экран-контекст (kind, ref): после ввода или отмены бот рисует его
    заново (back_to_context). Само приглашение — в служебные: убирается вместе
    с вводом человека."""
    await state.update_data(ctx_kind=kind, ctx_ref=int(ref or 0), **data)
    await edit(cb, prompt, kb.cancel_input(kind, ref))
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)


async def back_to_context(message: Message, services, data: dict, role: str, client=None,
                          note: str = "") -> None:
    """После ввода: убрать приглашение и ввод, показать экран-контекст новым
    сообщением с итогом первой строкой. Экрана нет (объект пропал, старый
    диалог) — главная роли."""
    from awgbot.bot import screens
    await cleanup_content(message.bot, services, message.chat.id)
    parts = await screens.render(data.get("ctx_kind") or "main", data.get("ctx_ref") or 0,
                                 services=services, role=role, client=client,
                                 chat_id=message.chat.id, note=note)
    if parts is None:
        parts = await screens.render("main", services=services, role=role, client=client,
                                     chat_id=message.chat.id, note=note)
    if parts is None:
        return
    await send_menu(message, services, *parts)


async def show_screen(message: Message, services, role: str, client, kind: str, ref: int = 0) -> bool:
    """Экран реестра по ссылке /start <payload>: команду из чата убрать (она
    служебная), экран — на месте живого меню, как переход по кнопке; новым
    сообщением — только если меню нет или его не отредактировать. False —
    такого экрана нет (чужой объект): вызывающий покажет главную."""
    from awgbot.bot import screens
    parts = await screens.render(kind, ref, services=services, role=role, client=client,
                                 chat_id=message.chat.id)
    if parts is None:
        return False
    try:
        await message.delete()
    except Exception:                                  # noqa: BLE001
        pass
    text, markup = parts
    nav_id = await call(services.db.get_nav_message_id, message.chat.id)
    if nav_id is not None:
        try:
            await message.bot.edit_message_text(text, chat_id=message.chat.id, message_id=nav_id,
                                                reply_markup=markup, link_preview_options=NO_PREVIEW)
            return True
        except Exception:                             # noqa: BLE001
            pass
    await send_menu(message, services, text, markup)
    return True


def role_of(client) -> str:
    """Роль по записи из middleware: admin (client=None), invited (гость), client."""
    if client is None:
        return "admin"
    return "invited" if getattr(client, "is_guest", False) else "client"


async def ask_tracked(message, services, text: str, **kw):
    """Отправить ПРОМЕЖУТОЧНОЕ служебное сообщение (вопрос FSM, переспрос,
    отбивку) и запомнить его id — при возврате в меню cleanup_content его сотрёт.
    Констатирующие результат сообщения так НЕ отправляем — они остаются следом."""
    sent = await message.answer(text, **kw)
    await _track_content(services, sent)
    return sent


async def park_screen(cb: CallbackQuery, services) -> None:
    """Экран под кнопкой отслужил, дальше — текстовый ввод: снять с него кнопки
    и записать в служебные (уборка при возврате в меню). Иначе пока человек
    печатает, в чате два живых экрана: этот и приглашение к вводу."""
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                  # noqa: BLE001
        pass
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)


async def cleanup_content(bot, services, chat_id: int) -> None:
    """Удалить ранее выданные контент-сообщения (ссылка/QR/файл + инструкции) —
    вызывается при возврате в меню, чтобы чат не захламлялся секретами."""
    ids = await call(services.db.pop_content_msg_ids, chat_id)
    await delete_many(bot, chat_id, ids)


async def content_finisher(message: Message, services, text: str, role: str,
                           markup=None) -> None:
    """Компактный баббл-«завершитель» ПОД выданным контентом: контекстный текст
    (что выше и что делать) + одна кнопка «В меню». Становится активным
    нав-сообщением (гасит прежнее). Кнопка ведёт в меню роли мутацией; markup —
    свой выход (например, «⬅️ В карточку» после нового приглашения).

    Модель: [контент-бабблы] → [этот завершитель с «В меню»]. Меню-простыню
    после контента не вываливаем — только выход."""
    from awgbot.bot import keyboards as _kb
    if markup is not None:
        pass
    elif role == "invited":
        markup = _kb.friend_finisher()
    else:
        markup = _kb.to_menu()                     # Menu(action="main") — admin/client
    await _dismiss_previous_nav(message.bot, services, message.chat.id)
    sent = await message.answer(text, reply_markup=markup)
    await call(services.db.set_nav_message_id, message.chat.id, sent.message_id)
    await _track_content(services, sent)     # финишер-инструкцию тоже убираем при возврате


async def edit_nav(cb: CallbackQuery, services, text, markup) -> None:
    """Навигация мутацией: редактируем текущее сообщение и делаем ЕГО активным
    нав-сообщением (гасим прежнее, если это было другое)."""
    chat_id = cb.message.chat.id
    cur_id = cb.message.message_id
    # один хоп: прежнее активное + запись нового + история в одной транзакции
    prev = await call(services.db.nav_touch, chat_id, cur_id)
    if prev is not None and prev != cur_id:
        try:
            await cb.message.bot.edit_message_reply_markup(chat_id=chat_id, message_id=prev,
                                                           reply_markup=None)
        except Exception:                                  # noqa: BLE001
            pass
    await edit(cb, text, markup)


# Чаты, где карточка слота шлюза открыта ссылкой с главного экрана: «Назад» с
# неё и со всех её подэкранов — на главную, откуда пришли. Пометка живёт до
# возврата на главную: любой другой вход в карточку (список, раздел, карточка
# устройства) начинается с главной, и выход снова обычный.
_card_home: set[int] = set()


def card_from_home(chat_id: int | None, yes: bool) -> None:
    (_card_home.add if yes else _card_home.discard)(chat_id)


def card_is_from_home(chat_id: int | None) -> bool:
    return chat_id in _card_home


async def show_main_menu(message: Message, services, role: str, client=None) -> None:
    """Показать главное меню роли новым сообщением (через send_menu — трекается,
    гасит прежнее активное). Экран — из реестра: у клиента с теми же кнопками
    РФ-доступа и выдачи, что и по кнопке «В меню»."""
    from awgbot.bot import screens
    card_from_home(message.chat.id, False)
    parts = await screens.render("main", services=services, role=role, client=client,
                                 chat_id=message.chat.id)
    if parts is None:
        return
    text, markup = parts
    # Возврат в меню = конец диалога: убираем все промежуточные служебные
    # сообщения (вопросы FSM, введённые пользователем значения, ссылки/QR).
    await cleanup_content(message.bot, services, message.chat.id)
    # меню шлём НОВЫМ сообщением через единую точку — она гасит прежнее активное
    # меню (инвариант «одно живое меню в чате»). reply-клаву в путях без lead уже
    # снял предыдущий контент; here markup — inline.
    await send_menu(message, services, text, markup)


async def edit(cb: CallbackQuery, text: str, kb=None) -> None:
    """Редактирует сообщение под инлайн-кнопкой.

    Различаем две ситуации TelegramBadRequest:
      • «message is not modified» — повторное нажатие той же кнопки; слать
        новое сообщение НЕЛЬЗЯ (задублируем меню) — тихо игнорируем;
      • прочее (сообщение слишком старое/удалено и т.п.) — шлём новое.
    """
    try:
        await cb.message.edit_text(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await cb.message.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


async def send_link(target: Message, vpn: str, services=None, *, name: str = "",
                    markup=None) -> Message:
    """vpn:// в моноширинном блоке (нажатие копирует) и пояснение одним
    сообщением: ссылка, пустая строка, «☝️ Ссылка для iPhone — …». markup —
    «⬅️ В меню» под тем же сообщением."""
    from awgbot.bot import texts
    text = f"<code>{vpn}</code>"
    if name:
        text += "\n\n" + texts.finish_link(name)
    sent = await target.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
    await _track_content(services, sent)
    return sent


async def send_conf(target: Message, name: str, conf: str, services=None, *,
                    markup=None, caption: str = "") -> Message:
    """.conf файлом; подпись — пояснение выдачи («📄 Для iPhone — …»)."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "config"
    doc = BufferedInputFile(conf.encode("utf-8"), filename=f"{safe}.conf")
    sent = await target.answer_document(doc, caption=caption or "📄 Файл конфигурации",
                                        reply_markup=markup)
    await _track_content(services, sent)
    return sent


async def send_qr(target: Message, vpn: str, services=None, *, markup=None,
                  caption: str = "") -> Message:
    """QR-код для импорта в AmneziaVPN — анимированным GIF (2 кадра серии).
    Шлём как фото/анимацию: Telegram автоплеит в ленте, получателю не нужно
    открывать файл. Двухкадровый QR ещё и нельзя снять одним скриншотом."""
    from awgbot.util import qrgen
    gif = await call(qrgen.vpn_link_to_qr_gif, vpn)
    media = BufferedInputFile(gif, filename="amnezia_qr.gif")
    sent = await target.answer_animation(media, caption=caption or "🔳 QR-код для AmneziaVPN",
                                         reply_markup=markup)
    await _track_content(services, sent)
    return sent


def own_device(services, client, device_id: int):
    """Устройство, только если принадлежит клиенту (защита от чужих id в callback).
    Синхронный — звать через call()."""
    dev = services.db.get_device(device_id)
    if dev is None or dev.client_id != client.id:
        return None
    return dev


def held_device(services, client, device_id: int):
    """Чужое устройство, которое клиент ДЕРЖИТ, или None."""
    dev = services.db.get_device(device_id)
    if dev is None or dev.holder_client_id != client.id:
        return None
    return dev


def mine_or_held(services, client, device_id: int):
    """Своё или удерживаемое — для выдачи конфига и карточки."""
    return own_device(services, client, device_id) or held_device(services, client, device_id)


async def send_device_config(target: Message, services, dev, kind: str,
                             *, finisher=None) -> None:
    """Единая точка «сгенерировать и отправить конфиг устройства».
    kind: link | file | qr | both. Поднимает ServiceError наверх (хендлер решает,
    как показать).

    finisher — клавиатура «⬅️ В меню»: тогда пояснение («☝️ Ссылка для
    iPhone — …») и кнопка едут в ОДНОМ сообщении с содержимым, и оно
    становится живым меню чата. Без finisher — голое содержимое (гайд ведёт
    дальше сам)."""
    from awgbot.bot import texts
    cfg = await call(services.generate_config, dev.id)
    sent = None
    if kind in ("link", "both"):
        sent = await send_link(target, cfg["vpn"], services,
                               name=dev.name if finisher else "", markup=finisher)
    if kind in ("file", "both"):
        sent = await send_conf(target, dev.name, cfg["conf"], services, markup=finisher,
                               caption=texts.finish_file(dev.name) if finisher else "")
    if kind == "qr":
        sent = await send_qr(target, cfg["vpn"], services, markup=finisher,
                             caption=texts.finish_qr(dev.name) if finisher else "")
    if finisher is not None and sent is not None:
        await _dismiss_previous_nav(target.bot, services, target.chat.id)
        await call(services.db.set_nav_message_id, target.chat.id, sent.message_id)


async def remove_device_and_notify(bot, services, device_id: int) -> None:
    """Удаляет устройство и, если его держал кто-то другой, уведомляет
    держателя, что доступ прекращён — с указанием, кто удалил: владелец или
    администратор. Обёртка над services.remove_device (тот возвращает tg
    держателя или None)."""
    from awgbot.bot import texts
    dev = await call(services.db.get_device, device_id)
    friend_tg = await call(services.remove_device, device_id)
    if friend_tg and dev is not None:
        await notify_one(bot, friend_tg, texts.lent_device_deleted_by_admin_notice(dev))


async def forget_secret(message: Message) -> None:
    """Убрать из чата присланный секрет: содержимое уже у нас в памяти, а в
    истории Telegram оно остаётся навсегда. В конфигурации шлюза — приватный
    ключ линка, токен агента, фраза шифрования копий и пароль почты; в
    резервной копии — вся база. Так же поступаем с токеном и паролем, которые
    админ присылает текстом."""
    try:
        await message.delete()
    except Exception:                                      # noqa: BLE001
        pass                                               # >48 ч, уже удалено


async def drop_message(cb: CallbackQuery) -> None:
    """Удалить сообщение под кнопкой (используется перед выдачей ссылки/файла,
    чтобы прежнее меню-с-кнопками не висело НАД присланной ссылкой). Если удалить
    нельзя (>48ч, уже удалено) — хотя бы снять кнопки."""
    try:
        await cb.message.delete()
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


__all__ = ["call", "edit", "drop_message", "send_link", "send_conf", "cleanup_content", "ask_tracked",
           "ask_here", "back_to_context", "show_screen", "role_of", "park_screen", "purge_menus", "dismiss_update_reports",
           "own_device", "held_device", "mine_or_held", "send_device_config"]
