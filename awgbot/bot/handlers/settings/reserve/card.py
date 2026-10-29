"""card.py — раздел «🛟 Резерв»: экран без пары (Т-Р1) и карточка резерва
(Т-Р9), тумблеры «Готов быть резервом» (выключение при паре — через диалог
Т-Р14) и «Мои шлюзы для резерва», рычаг возврата (Т-Р15), «🆘 На крайний
случай» (Т-Р18: показать, сменить), «🗑 Удалить резерв» (Т-Р13)."""
from __future__ import annotations

import logging

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from awgbot.bot import ui
from awgbot.bot.callbacks import RsvCB
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.texts.reserve import notices, section as st_t
from awgbot.core import settings
from awgbot.domain.services import ServiceError

from .._router import router
from ..render import _render
from .events import send_code

log = logging.getLogger("awgbot.handlers.settings")


async def reserve_screen(services):
    """(text, markup) раздела: без пары — Т-Р1, с парой — карточка Т-Р9."""
    if await call(services.reserve_paired):
        d = await call(services.reserve_card_data)
        return st_t.card_text(d), rkb.card(ready=d["ready"], rf_on=d["rf_on"], has_gateway=d["has_gateway"],
                                           ret_immediate=d["ret_immediate"], name=(d.get("peer") or {}).get("name") or "резерв",
                                           can_move=not d.get("move_why"), can_evict=not d.get("evict_why"),
                                           can_return=await call(services.reserve_can_return),
                                           self_admin=bool(d.get("self_admin")), can_force=not d.get("force_why"))
    d = await call(services.reserve_section_data)
    return st_t.section_text(d), rkb.section(d["ready"], invite_live=bool(d["invite_live"]), accepting=bool(d["accepting"]))


async def _set(cb: CallbackQuery, key: str, value) -> bool:
    try:
        await call(settings.set_value, key, value)
        return True
    except settings.SettingsWriteError as e:
        await cb.answer(ui.toast_cap(e, html=getattr(e, "html", False)), show_alert=True)
        return False


@router.callback_query(RsvCB.filter(F.action == "ready"))
async def ready_toggle(cb: CallbackQuery, services, state: FSMContext):
    on = settings.get_bool("app.reserve.ready", False)
    if on and await call(services.reserve_paired):
        await edit(cb, st_t.ready_off_dialog(await call(services.reserve_peer_name)), rkb.confirm_ready_off())
        await cb.answer()
        return
    if not await _set(cb, "app.reserve.ready", not on):
        return
    tools = ""
    if not on:
        tools = await call(services.reserve_tools_ensure)      # dnsmasq и conntrack — сами, в фоне
    await _render(cb, "rsv", services)
    await cb.answer(st_t.ready_on_toast(tools) if not on else "Готовность выключена")
    from awgbot.runtime import pairlink
    await pairlink.send("st", await call(services.reserve_st_body))


@router.callback_query(RsvCB.filter(F.action == "ready_off_yes"))
async def ready_off_yes(cb: CallbackQuery, services):
    if not await _set(cb, "app.reserve.ready", False):
        return
    await _render(cb, "rsv", services)
    busy = await call(services.reserve_busy)
    await cb.answer("Готовность выключена" + (" — вступит после возврата" if busy else ""))
    from awgbot.runtime import pairlink
    await pairlink.send("st", await call(services.reserve_st_body))


@router.callback_query(RsvCB.filter(F.action == "rfmine"))
async def rf_mine_toggle(cb: CallbackQuery, services):
    on = settings.get_bool("app.reserve.rf_via_mine", True)
    if not await _set(cb, "app.reserve.rf_via_mine", not on):
        return
    await _render(cb, "rsv", services)
    await cb.answer("Мои шлюзы для резерва: " + ("выкл" if on else "вкл"))


@router.callback_query(RsvCB.filter(F.action == "ret"))
async def return_lever(cb: CallbackQuery, services):
    cur = str(settings.get("app.reserve.return", "immediate") or "immediate")
    nxt = "button" if cur == "immediate" else "immediate"
    if not await _set(cb, "app.reserve.return", nxt):
        return
    await _render(cb, "rsv", services)
    await cb.answer("Возврат пользователей: сразу" if nxt == "immediate" else "Возврат пользователей: по кнопке")


@router.callback_query(RsvCB.filter(F.action == "role"))
async def role_screen(cb: CallbackQuery, services):
    """Общий админ обоих серверов: экран роли «🛰 Для «Alpha»» — разделом карточки."""
    from awgbot.bot.texts.reserve import peeradmin as pt
    d = await call(services.reserve_peer_admin_summary)
    await edit(cb, pt.screen(d), rkb.peer_admin(code_needed=bool(d.get("code_needed")), in_card=True))
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "emerg"))
async def emergency(cb: CallbackQuery, services):
    keys = await call(services.reserve_keys)
    peer = await call(services.reserve_peer) or {}
    bot_user = str(peer.get("bot_username") or "").lstrip("@")
    await edit(cb, st_t.emergency_text(bool(keys.get("m")), str(keys.get("code_shown_at") or "")),
               rkb.emergency(bool(keys.get("m")), peer_bot_url=f"https://t.me/{bot_user}" if bot_user else "",
                             peer_name=str(peer.get("name") or "")))
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "show_code"))
async def show_code(cb: CallbackQuery, services):
    code = await call(services.reserve_code)
    if not code:
        await cb.answer("Кода нет — он появится после подключения резерва", show_alert=True)
        return
    await send_code(cb.bot, services)
    await cb.answer("Код отправлен отдельным сообщением")


@router.callback_query(RsvCB.filter(F.action == "new_code"))
async def new_code_ask(cb: CallbackQuery, services):
    if not await call(services.reserve_paired):
        await cb.answer("Резерва нет", show_alert=True)
        return
    await edit(cb, st_t.new_code_dialog(await call(services.reserve_peer_name)), rkb.confirm_new_code())
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "new_code_yes"))
async def new_code(cb: CallbackQuery, services):
    if not await call(services.reserve_paired):
        await cb.answer("Резерва нет", show_alert=True)
        return
    try:
        await call(services.reserve_new_generation, "по кнопке «🔄 Сменить код»")
    except ServiceError as e:
        await cb.answer(ui.toast_cap(e, html=getattr(e, "html", False)), show_alert=True)
        return
    await send_code(cb.bot, services, renewed_why="по кнопке «🔄 Сменить код»")
    await cb.answer("Ключ переезда и код сменены")
    keys = await call(services.reserve_keys)
    await edit(cb, st_t.emergency_text(True, str(keys.get("code_shown_at") or "")), rkb.emergency(True))


@router.callback_query(RsvCB.filter(F.action == "unpair"))
async def unpair_ask(cb: CallbackQuery, services):
    why = await call(services.reserve_unpair_check)
    if why:
        await cb.answer(ui.toast_cap(why), show_alert=True)
        return
    await edit(cb, st_t.unpair_dialog(await call(services.reserve_peer_name)), rkb.confirm_unpair())
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "unpair_yes"))
async def unpair_yes(cb: CallbackQuery, services):
    from awgbot.runtime import pairlink
    await pairlink.send("gone")
    try:
        peer = await call(services.reserve_unpair)
    except ServiceError as e:
        await cb.answer(ui.toast_cap(e, html=getattr(e, "html", False)), show_alert=True)
        return
    await pairlink.shutdown()
    await _render(cb, "rsv", services)
    await cb.message.answer(notices.unpaired(peer.get("name") or "резерв"))
    await cb.answer("Резерв удалён")
