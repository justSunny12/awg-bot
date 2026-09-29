"""
handlers/admin/panel.py — панель администратора.

/start админа (в том числе ссылки на экраны панели), главная, списки онлайн,
истекающих, без профиля, трафик списком; общие помощники панели
(_panel_parts / _return_panel / _main_menu_markup / restore_panel_after_restart),
которыми пользуются остальные роутеры пакета.
"""

from __future__ import annotations

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import Menu
from awgbot.bot.handlers.common import (call, edit_nav, purge_menus, cleanup_content, send_menu, card_from_home,
                                        _dismiss_previous_nav, show_screen)
from awgbot.bot.notifier import send_notifications

router = Router(name="admin.panel")


def _bot(services) -> str:
    return getattr(services, "bot_username", "") or ""


def _menu_markup_from(snap: dict):
    ac = snap["ac"]
    return kb.admin_main(gateways=bool(snap.get("gateways")), routing_visible=snap["rt_visible"],
                         self_client_id=(ac.id if ac else 0))


def _panel_text_from(services, snap: dict) -> str:
    return texts.admin_panel(snap["st"], snap["routing_ok"], migration=snap["mig"], rf=snap.get("rf"),
                             bot_username=_bot(services), expiring=snap["expiring"],
                             routing_info=snap.get("routing_info"), unassigned=snap.get("unassigned", 0),
                             update_tag=snap.get("update_tag", ""))


async def _panel_parts(services):
    """(текст, клавиатура) главной — из ОДНОГО снимка в одном потоке."""
    snap = await call(services.admin_panel_snapshot)
    return _panel_text_from(services, snap), _menu_markup_from(snap)


async def _main_menu_markup(services):
    return _menu_markup_from(await call(services.admin_panel_snapshot))


async def _return_panel(message, services, keep_id=None) -> None:
    """Показать главную новым сообщением — единый «выход» из любого диалога.
    Гасит прежнее активное меню и стирает служебные сообщения диалога."""
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, *await _panel_parts(services), keep_id=keep_id)


async def restore_panel_after_restart(bot, services) -> None:
    """Исполнить обещание «вернётся через несколько секунд»: отчёт на месте
    обещания (остаётся в чате), главная — следующим сообщением."""
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


# ── экраны шапки ─────────────────────────────────────────────────────────────

async def expiring_screen(services):
    rows = await call(services.expiring_subscriptions)
    return texts.expiring_text(rows, _bot(services)), kb.expiring_kb(rows)


_expiring_screen = expiring_screen


async def online_screen(services):
    devs = await call(services.online_devices)
    return texts.online_devices_text(devs, _bot(services)), kb.online_devices_kb()


_online_screen = online_screen


async def unassigned_screen(services, chat_id: int = 0):
    service_id = await call(services.db.get_service_client_id)
    devices = await call(services.db.list_devices, service_id)
    from awgbot.bot import paging
    return (texts.unassigned_text(len(devices)),
            kb.unassigned_devices(devices, page=paging.page_of(chat_id or config.ADMIN_ID, "unassigned")))


async def traffic_profiles_screen(services):
    rows = await call(services.traffic_by_profile)
    tot = await call(services.db.get_total_month_traffic)
    rf = await call(services.rf_screen_data)
    rf_total = (rf["rx"], rf["tx"]) if await call(services.rf_line_visible) else None
    return (texts.traffic_profiles_text(rows, _bot(services), (int(tot["rx"]), int(tot["tx"])),
                                        rf_total=rf_total, outside=rf.get("outside", 0)),
            kb.traffic_profiles_kb())


_traffic_profiles_screen = traffic_profiles_screen


async def traffic_devices_screen(services, client_id: int):
    client = await call(services.db.get_client, client_id)
    if client is None:
        return None
    rows = await call(services.traffic_by_device, client_id)
    t = await call(services.db.get_client_traffic, client_id)
    rf = await call(services.db.get_client_rf, client_id)
    rf_total = rf if await call(services.rf_client_visible, client, rf) else None
    return (texts.traffic_devices_text(client, rows, (int(t["rx_month"]), int(t["tx_month"])),
                                       rf_total=rf_total, bot_username=_bot(services)),
            kb.traffic_devices_kb())


_traffic_devices_screen = traffic_devices_screen


async def migration_overview_screen(services):
    d = await call(services.migration_overview)
    return texts.migration_overview_text(d, _bot(services)), kb.to_menu_kb()


async def migration_client_screen(services, client_id: int):
    client = await call(services.db.get_client, client_id)
    if client is None:
        return None
    rows = await call(services.migration_client_devices, client_id)
    return texts.migration_client_text(client, rows, _bot(services)), kb.migration_back_kb()


async def gateway_card_screen(services, slot: int, chat_id: int | None = None):
    """Карточка слота по ссылке с главной: «Назад» с неё — на главную."""
    from awgbot.domain.services import ServiceError
    from awgbot.bot.handlers.settings import card_kb
    try:
        st = await call(services.gateway_screen_state, slot)
    except ServiceError:
        return "🛰 Такого шлюза больше нет — слот снят", kb.settings_back("rt")
    card_from_home(chat_id, True)
    return texts.gateway_card_text(st, st["states"]), card_kb(st, chat_id)


_gateway_card_screen = gateway_card_screen


def _payload_id(payload: str, head: str) -> int | None:
    """«head-<id>» или «head-<id>-t» → id."""
    if not payload.startswith(head + "-"):
        return None
    rest = payload[len(head) + 1:]
    if rest.endswith("-t"):
        rest = rest[:-2]
    return int(rest) if rest.isdigit() else None


GWCFG_PAYLOAD = "gwcfg"          # /start gwcfg-<слот> — перевыпуск конфигурации слота


def parse_link(payload: str) -> tuple[str, int] | None:
    """Ссылка шапки и уведомлений → (вид экрана реестра, ref) или None."""
    if payload == texts.ONLINE_PAYLOAD:
        return "online", 0
    if payload == texts.EXPIRING_PAYLOAD:
        return "expiring", 0
    if payload == texts.UNASSIGNED_PAYLOAD:
        return "unassigned", 0
    if payload == texts.UPD_PAYLOAD:
        return "upd", 0
    if payload in (texts.TRAFFIC_PAYLOAD, texts.RF_PAYLOAD):
        return "traffic", 0
    if payload == texts.MIGRATION_PAYLOAD:
        return "migration", 0
    for head, kind in ((texts.TRAFFIC_PAYLOAD, "traffic_dev"), (texts.RF_PAYLOAD, "traffic_dev"),
                       ("extend", "extend"), (texts.GW_CARD_PAYLOAD, "gw"), ("cl", "cl"), ("dev", "dev"),
                       (texts.MIGRATION_PAYLOAD, "migration_cl"), (GWCFG_PAYLOAD, "gwcfg")):
        ref = _payload_id(payload, head)
        if ref is not None:
            return kind, ref
    return None


async def _traffic_deep_link(message: Message, services, payload: str,
                             state: FSMContext | None = None) -> bool:
    """Переход по ссылке из шапки, экрана или уведомления: экран — на месте
    живого меню, команда убирается из чата."""
    link = parse_link(payload)
    if link is None:
        return False
    kind, ref = link
    if kind == "extend" and state is not None:
        await state.update_data(return_to="expiring")
    if kind == "gwcfg":
        # «Перевыпусти» из уведомления — файл конфигурации слота сразу
        from awgbot.bot.handlers.settings import send_gw_bundle
        try:
            await message.delete()
        except Exception:                             # noqa: BLE001
            pass
        await send_gw_bundle(message, services, ref)
        return True
    if not await show_screen(message, services, "admin", None, kind, ref):
        await _return_panel(message, services)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# /start и главная
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


@router.callback_query(Menu.filter(F.action == "migration"))
async def admin_migration_overview(cb: CallbackQuery, services):
    await edit_nav(cb, services, *await migration_overview_screen(services))
    await cb.answer()


@router.message(F.document, StateFilter(None))
async def admin_document(message: Message, services, state: FSMContext):
    """Файл в чате без активного диалога: резервная копия → предложить
    восстановление. Прочие файлы молча не трогаем."""
    from awgbot.bot.handlers import restore as rs
    if rs.looks_like_backup(message.document):
        await rs.offer_restore(message, services, state, gateway=False)


@router.callback_query(Menu.filter(F.action == "expiring"))
async def admin_expiring(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await edit_nav(cb, services, *await expiring_screen(services))
    await cb.answer()


@router.callback_query(Menu.filter(F.action.in_(("traffic", "traffic_local"))))
async def admin_traffic_profiles(cb: CallbackQuery, services):
    """«Назад» из разбивки по устройствам — в список по профилям (прежние
    кнопки «РФ-доступ» ведут туда же)."""
    await edit_nav(cb, services, *await traffic_profiles_screen(services))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "online"))
async def admin_online(cb: CallbackQuery, services):
    await edit_nav(cb, services, *await online_screen(services))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "unassigned"))
async def unassigned_list(cb: CallbackQuery, services):
    await edit_nav(cb, services, *await unassigned_screen(services, cb.message.chat.id))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "main"))
async def admin_main_menu(cb: CallbackQuery, services, state: FSMContext):
    await cb.answer()                                  # спиннер гаснет сразу
    await state.clear()
    card_from_home(cb.message.chat.id, False)          # с главной карточка открывается заново
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    await edit_nav(cb, services, *await _panel_parts(services))


@router.callback_query(Menu.filter(F.action == "refresh"))
async def refresh_status(cb: CallbackQuery, services):
    """Внеплановое обновление статуса и метрик по кнопке: разово дёргает
    сервер и /proc, пишет в state, перерисовывает главную."""
    await cb.answer("Обновляю…")
    notes = await call(services.refresh_status_now)
    await edit_nav(cb, services, *await _panel_parts(services))
    await send_notifications(cb.bot, notes)
