"""
handlers/admin/panel.py — панель администратора.

/start админа (в том числе deep-link'и на экраны панели), главное меню,
списки истекающих и потребления, обновление статуса; общие помощники панели
(_panel_parts / _return_panel / _main_menu_markup / restore_panel_after_restart),
которыми пользуются остальные роутеры пакета.
"""

from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import Menu
from awgbot.bot.handlers.common import (call, edit_nav, purge_menus, cleanup_content, send_menu, card_from_home,
                                        _dismiss_previous_nav)
from awgbot.bot.notifier import send_notifications
from awgbot.domain.services import SECONDS_PER_DAY

router = Router(name="admin.panel")


def _menu_markup_from(snap: dict):
    ac = snap["ac"]
    # админ — такой же пользователь VPN: режим ему нужен в главном меню, рядом
    # со своими устройствами. Разрешение у него по умолчанию (routing_allowed_for)
    return kb.admin_main(snap["unassigned"], self_has_devices=snap["has_dev"],
                         self_can_issue=snap["can_issue"],
                         routing_visible=snap["rt_visible"], routing_on=snap["rt_on"],
                         self_client_id=(ac.id if ac else 0))


def _panel_text_from(services, snap: dict) -> str:
    return texts.admin_panel(snap["st"], snap["routing_ok"], migration=snap["mig"], rf=snap.get("rf"),
                             bot_username=getattr(services, "bot_username", ""),
                             expiring=snap["expiring"], routing_info=snap.get("routing_info"))


async def _panel_parts(services):
    """(текст, клавиатура) панели — из ОДНОГО снимка в одном потоке.
    Раньше — 12–15 хопов и ~20 запросов на каждое нажатие «В меню»."""
    snap = await call(services.admin_panel_snapshot)
    return _panel_text_from(services, snap), _menu_markup_from(snap)


async def _main_menu_markup(services):
    return _menu_markup_from(await call(services.admin_panel_snapshot))


async def _return_panel(message, services, keep_id=None) -> None:
    """Показать админ-панель новым сообщением — единый «выход» из любого диалога,
    чтобы юзер не оставался без навигации. Гасит прежнее активное меню и стирает
    промежуточные служебные сообщения диалога (вопросы, ввод, ссылки). keep_id —
    итог, у которого кнопки уже сняты вызывающим."""
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, *await _panel_parts(services), keep_id=keep_id)


async def restore_panel_after_restart(bot, services) -> None:
    """Исполнить обещание «вернётся через несколько секунд». Зовётся новым
    процессом на старте.

    Обещание давал уходящий процесс, а исполнить его было некому: после старта в
    чат никто не пишет, и админ оставался с мёртвым сообщением без кнопок, пока
    сам не отправлял /start.

    Обещание подменяется отчётом на том же месте и ОСТАЁТСЯ в чате — это
    единственный след того, что перезапуск состоялся, а не завис. Панель идёт
    следующим сообщением: неси она свои кнопки на отчёте, в чате оказалось бы
    два живых меню, и инвариант «одно активное» держать было бы нечем.
    """
    waiting = await call(services.pop_restart_wait)
    if waiting is None:
        return
    chat_id, mid = waiting
    try:
        await bot.edit_message_text(texts.BOT_RESTARTED, chat_id=chat_id,
                                    message_id=mid, reply_markup=None)
    except Exception:                                  # noqa: BLE001
        pass          # сообщение удалили — отчёт потерян, панель важнее
    await _dismiss_previous_nav(bot, services, chat_id)
    from awgbot.bot.handlers.common import NO_PREVIEW
    text, markup = await _panel_parts(services)
    sent = await bot.send_message(chat_id, text, reply_markup=markup,
                                  link_preview_options=NO_PREVIEW)
    await call(services.db.nav_touch, chat_id, sent.message_id)


async def _expiring_screen(services):
    rows = await call(services.expiring_subscriptions)
    return (texts.expiring_text(rows, getattr(services, "bot_username", "")),
            kb.expiring_kb())


async def _extend_picker(services, client_id: int, cancel_to: str | None = None):
    """Экран «На какой срок продлить?» — общий для карточки и списка истекающих."""
    client = await call(services.db.get_client, client_id)
    if client is None:
        return None
    cut_days = int(client.grace_pending_cut) // SECONDS_PER_DAY
    text = "На какой срок продлить?"
    if cut_days > 0:
        text = (f"⚠️ Профиль брал отсрочку на {cut_days} дн. — она вычтется из "
                f"нового периода. Доступны только периоды длиннее {cut_days} дн.\n\n"
                "На какой срок продлить?")
    return text, kb.period_choices("extend", ref=client_id, min_days=cut_days,
                                   cancel_to=cancel_to)


# ── потребление за месяц: по профилям → по устройствам ───────────────────────

_TRAFFIC_PAYLOAD = "traffic"


async def _traffic_profiles_screen(services):
    rows = await call(services.traffic_by_profile)
    tot = await call(services.db.get_total_month_traffic)
    return (texts.traffic_profiles_text(rows, getattr(services, "bot_username", ""),
                                        (int(tot["rx"]), int(tot["tx"]))),
            kb.traffic_profiles_kb())


async def _traffic_devices_screen(services, client_id: int):
    client = await call(services.db.get_client, client_id)
    if client is None:
        return None
    rows = await call(services.traffic_by_device, client_id)
    t = await call(services.db.get_client_traffic, client_id)
    return (texts.traffic_devices_text(client.name, rows, (int(t["rx_month"]), int(t["tx_month"]))),
            kb.traffic_devices_kb())


# ── РФ-доступ за месяц: по профилям → по устройствам (концепт «учёт РФ-трафика») ──

async def _rf_profiles_screen(services):
    data = await call(services.rf_screen_data)
    return (texts.rf_profiles_text(data, getattr(services, "bot_username", "")),
            kb.rf_profiles_kb())


async def _rf_devices_screen(services, client_id: int, back: str = "traffic_local"):
    """Разбивка РФ профиля; back — откуда пришли («Назад» туда же): с экрана
    РФ-доступа (traffic_local) или из списка трафика по профилям (traffic)."""
    client = await call(services.db.get_client, client_id)
    if client is None:
        return None
    rows = await call(services.rf_by_device, client_id)
    t = await call(services.db.get_client_rf, client_id)
    return (texts.rf_devices_text(client.name, rows, (int(t["rx"]), int(t["tx"]))),
            kb.rf_devices_kb(back))


async def _gateway_card_screen(services, slot: int, chat_id: int | None = None):
    """Карточка слота по ссылке с главного экрана: «Назад» с неё — на главную,
    не в список шлюзов, откуда человек не приходил."""
    from awgbot.domain.services import ServiceError
    from awgbot.bot.handlers.settings import card_kb
    try:
        # пинг — как по кнопке «Карточка»: лениво, пустой кэш заполняется
        # замером; без него неизмеренный пинг читался бы как «шлюз не отвечает»
        st = await call(services.gateway_screen_state, slot)
    except ServiceError:
        # ссылка из старой шапки, слот с тех пор сняли: свой ответ, а не
        # «профиль не найден» с клавиатурой потребления
        return "🛰 Такого шлюза больше нет — слот снят", kb.settings_back("rt")
    card_from_home(chat_id, True)
    return texts.gateway_card_text(st, st["states"]), card_kb(st, chat_id)


async def _online_screen(services):
    devs = await call(services.online_devices)
    return texts.online_devices_text(devs), kb.online_devices_kb()


def _rf_payload_id(payload: str) -> int | None:
    """traffic_local-<id> | traffic_local-<id>-t → id."""
    rest = payload[len(texts.RF_PAYLOAD) + 1:]
    if rest.endswith("-t"):
        rest = rest[:-2]
    return int(rest) if rest.isdigit() else None


async def _traffic_deep_link(message: Message, services, payload: str,
                             state: FSMContext | None = None) -> bool:
    """«/start traffic», «/start traffic-<id>», «/start online», «/start
    expiring», «/start extend-<id>», «/start gw-<слот>» — переходы по ссылкам
    из панели и её экранов. Команду, которую отправил клик, убираем из чата:
    она служебная."""
    if payload == "online":
        screen = await _online_screen(services)
    elif payload == "expiring":
        screen = await _expiring_screen(services)
    elif payload.startswith("extend-") and payload[len("extend-"):].isdigit():
        # «Продлить?» из списка истекающих: после продления или отмены —
        # обратно в список (если он не опустел), иначе в меню
        if state is not None:
            await state.update_data(return_to="expiring")
        screen = await _extend_picker(services, int(payload[len("extend-"):]),
                                      cancel_to=Menu(action="expiring").pack())
    elif payload.startswith(texts.GW_CARD_PAYLOAD + "-") and payload[len(texts.GW_CARD_PAYLOAD) + 1:].isdigit():
        # имя шлюза или «резерв жив» в строке РФ-доступа — карточка слота
        screen = await _gateway_card_screen(services, int(payload[len(texts.GW_CARD_PAYLOAD) + 1:]),
                                            message.chat.id)
    elif payload == texts.RF_PAYLOAD:
        screen = await _rf_profiles_screen(services)
    elif payload.startswith(texts.RF_PAYLOAD + "-") and _rf_payload_id(payload) is not None:
        # «…-t» — пришли из списка трафика по профилям: «Назад» туда же
        screen = await _rf_devices_screen(services, _rf_payload_id(payload),
                                          "traffic" if payload.endswith("-t") else "traffic_local")
    elif payload == _TRAFFIC_PAYLOAD:
        screen = await _traffic_profiles_screen(services)
    elif payload.startswith(_TRAFFIC_PAYLOAD + "-") and payload[len(_TRAFFIC_PAYLOAD) + 1:].isdigit():
        screen = await _traffic_devices_screen(services, int(payload[len(_TRAFFIC_PAYLOAD) + 1:]))
    else:
        return False
    try:
        await message.delete()
    except Exception:                                 # noqa: BLE001
        pass
    text, markup = screen if screen is not None else ("Профиль не найден.", kb.traffic_profiles_kb())
    # Экран — НА МЕСТЕ активного меню, как переход по кнопке: панель исчезает,
    # «В меню» возвращает её туда же. Новым сообщением — только если активного
    # меню нет или его уже не отредактировать.
    from awgbot.bot.handlers.common import NO_PREVIEW
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


# ─────────────────────────────────────────────────────────────────────────────
# /start и главное меню
# ─────────────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def admin_start(message: Message, services, state: FSMContext,
                      command: CommandObject | None = None):
    await state.clear()
    payload = ((command.args if command is not None else "") or "").strip()
    if payload and await _traffic_deep_link(message, services, payload, state):
        return
    # /start — «начать заново»: все прошлые меню из чата долой, не только кнопки
    await purge_menus(message.bot, services, message.chat.id)
    await _return_panel(message, services)


@router.message(F.document, StateFilter(None))
async def admin_document(message: Message, services, state: FSMContext):
    """Файл в чате без активного диалога: резервная копия → предложить
    восстановление. Прочие файлы молча не трогаем."""
    from awgbot.bot.handlers import restore as rs
    if rs.looks_like_backup(message.document):
        await rs.offer_restore(message, services, state, gateway=False)


@router.callback_query(Menu.filter(F.action == "expiring"))
async def admin_expiring(cb: CallbackQuery, services, state: FSMContext):
    """Список истекающих: «Отмена» из продления и повторный вход."""
    await state.clear()
    await edit_nav(cb, services, *await _expiring_screen(services))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "traffic"))
async def admin_traffic_profiles(cb: CallbackQuery, services):
    """«Назад» из разбивки по устройствам — в разбивку по профилям."""
    await edit_nav(cb, services, *await _traffic_profiles_screen(services))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "traffic_local"))
async def admin_rf_profiles(cb: CallbackQuery, services):
    """«Назад» с РФ-доступа профиля — на экран по профилям."""
    await edit_nav(cb, services, *await _rf_profiles_screen(services))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "main"))
async def admin_main_menu(cb: CallbackQuery, services, state: FSMContext):
    await cb.answer()                                  # спиннер гаснет сразу
    await state.clear()
    card_from_home(cb.message.chat.id, False)          # с главной карточка открывается заново
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    await edit_nav(cb, services, *await _panel_parts(services))


# ─────────────────────────────────────────────────────────────────────────────
# Сервер: статус / бэкап / перезапуск
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "refresh"))
async def refresh_status(cb: CallbackQuery, services):
    """Внеплановое обновление статуса/метрик по кнопке (только админ — весь
    роутер под RoleFilter). Дёргает контейнер и /proc разово, пишет в state,
    затем перерисовывает панель из свежего state."""
    await cb.answer("Обновляю…")
    notes = await call(services.refresh_status_now)
    await edit_nav(cb, services, *await _panel_parts(services))
    await send_notifications(cb.bot, notes)
