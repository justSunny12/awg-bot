"""
handlers/common.py — общие помощники для роутеров.

Здесь: отправка конфига (ссылка/файл), безопасное редактирование сообщений,
и обёртка вызова синхронных services через to_thread.
"""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest

from aiogram.types import BufferedInputFile, CallbackQuery, Message

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


async def send_menu(message: Message, services, text, markup) -> None:
    """Показать меню/нав-экран НОВЫМ сообщением, погасив предыдущее активное.
    Единая точка показа — держит инвариант «одно живое меню в чате»."""
    chat_id = message.chat.id
    await _dismiss_previous_nav(message.bot, services, chat_id)
    sent = await message.answer(text, reply_markup=markup)
    await call(services.db.set_nav_message_id, chat_id, sent.message_id)


async def _track_content(services, sent) -> None:
    """Запомнить id контент-сообщения (ссылка/QR/файл) для удаления при возврате."""
    if services is None or sent is None:
        return
    await call(services.db.add_content_msg_id, sent.chat.id, sent.message_id)


async def ask_tracked(message, services, text: str, **kw):
    """Отправить ПРОМЕЖУТОЧНОЕ служебное сообщение (вопрос FSM, переспрос,
    отбивку) и запомнить его id — при возврате в меню cleanup_content его сотрёт.
    Констатирующие результат сообщения так НЕ отправляем — они остаются следом."""
    sent = await message.answer(text, **kw)
    await _track_content(services, sent)
    return sent


async def cleanup_content(bot, services, chat_id: int) -> None:
    """Удалить ранее выданные контент-сообщения (ссылка/QR/файл + инструкции) —
    вызывается при возврате в меню, чтобы чат не захламлялся секретами."""
    ids = await call(services.db.pop_content_msg_ids, chat_id)
    for mid in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:                    # noqa: BLE001 — сообщение могло уйти/устареть
            pass


async def content_finisher(message: Message, services, text: str, role: str) -> None:
    """Компактный баббл-«завершитель» ПОД выданным контентом: контекстный текст
    (что выше и что делать) + одна кнопка «В меню». Становится активным
    нав-сообщением (гасит прежнее). Кнопка ведёт в меню роли мутацией.

    Модель: [контент-бабблы] → [этот завершитель с «В меню»]. Меню-простыню
    после контента не вываливаем — только выход."""
    from awgbot.bot import keyboards as _kb
    if role == "invited":
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
    await _dismiss_previous_nav(cb.message.bot, services, chat_id, keep_id=cur_id)
    await edit(cb, text, markup)
    await call(services.db.set_nav_message_id, chat_id, cur_id)


async def show_main_menu(message: Message, services, role: str, client=None) -> None:
    """Показать главное меню роли новым сообщением (через send_menu — трекается,
    гасит прежнее активное). Ленивый импорт ролевых рендереров — общий модуль
    не тянет хендлеры на уровне модуля."""
    if role == "admin":
        from awgbot.bot.handlers.admin import _panel_text, _main_menu_markup
        text = await _panel_text(services)
        markup = await _main_menu_markup(services)
    elif role == "client":
        from awgbot.bot.handlers.client import _greeting
        text, (used, _) = await _greeting(services, client)
        markup = kb.client_main(has_devices=used > 0)
    elif role == "invited":
        from awgbot.bot.handlers.friend import friend_panel_payload
        text, markup = await friend_panel_payload(services, message.from_user.id)
    else:
        return
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
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await cb.message.answer(text, reply_markup=kb)


async def send_link(target: Message, vpn: str, services=None) -> None:
    """vpn:// строкой в моноширинном блоке (одно нажатие — копирование)."""
    sent = await target.answer(
        "🔗 Ссылка для подключения (нажми, чтобы скопировать):\n"
        f"<code>{vpn}</code>"
    )
    await _track_content(services, sent)


async def send_conf(target: Message, name: str, conf: str, services=None) -> None:
    """.conf файлом."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "config"
    doc = BufferedInputFile(conf.encode("utf-8"), filename=f"{safe}.conf")
    sent = await target.answer_document(doc, caption="📄 Файл конфигурации")
    await _track_content(services, sent)


async def send_qr(target: Message, vpn: str, services=None) -> None:
    """QR-код для импорта в AmneziaVPN — анимированным GIF (2 кадра серии).
    Шлём как фото/анимацию: Telegram автоплеит в ленте, получателю не нужно
    открывать файл. Двухкадровый QR ещё и нельзя снять одним скриншотом."""
    from awgbot.util import qrgen
    gif = await call(qrgen.vpn_link_to_qr_gif, vpn)
    media = BufferedInputFile(gif, filename="amnezia_qr.gif")
    sent = await target.answer_animation(
        media,
        caption="🔳 QR-код для AmneziaVPN")
    await _track_content(services, sent)


def own_device(services, client, device_id: int):
    """Устройство, только если принадлежит клиенту (защита от чужих id в callback).
    Синхронный — звать через call()."""
    dev = services.db.get_device(device_id)
    if dev is None or dev.client_id != client.id:
        return None
    return dev


async def send_device_config(target: Message, services, dev, kind: str) -> None:
    """Единая точка «сгенерировать и отправить конфиг устройства».
    kind: link | file | qr | both. Поднимает ServiceError наверх (хендлер решает,
    как показать)."""
    cfg = await call(services.generate_config, dev.id)
    if kind in ("link", "both"):
        await send_link(target, cfg["vpn"], services)
    if kind in ("file", "both"):
        await send_conf(target, dev.name, cfg["conf"], services)
    if kind == "qr":
        await send_qr(target, cfg["vpn"], services)


async def remove_device_and_notify(bot, services, device_id: int) -> None:
    """Удаляет устройство и, если у него был активный друг, уведомляет его, что
    доступ прекращён. Обёртка над services.remove_device (тот возвращает
    friend_tg_id или None)."""
    from awgbot.bot import texts
    friend_tg = await call(services.remove_device, device_id)
    if friend_tg:
        await notify_one(bot, friend_tg, texts.FRIEND_DEVICE_DELETED_GENERIC)


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
           "own_device", "send_device_config"]
