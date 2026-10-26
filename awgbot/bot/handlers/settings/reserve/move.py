"""move.py — переезд кнопкой и возврат в карточке резерва: «🔀 Переезд на
«Beta»» → диалог (Т-Р10) → живое сообщение хода (Т-Р10б; его правят такты
автомата через `refresh_live`); «↩️ Вернуть пользователей» (основа, рычаг
«по кнопке»); «↩️ Вернуть всё на «Alpha»» (резерв, Т-Р12 → `evict`)."""
from __future__ import annotations

import logging

from aiogram import F
from aiogram.types import CallbackQuery

from awgbot.bot import ui
from awgbot.bot.callbacks import RsvCB
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.texts.reserve import section as st_t
from awgbot.core import config
from awgbot.domain.services import ServiceError

from .._router import router
from ..render import _render

log = logging.getLogger("awgbot.handlers.settings")


async def _run(actions) -> None:
    from awgbot.runtime import rsvactions
    await rsvactions.run(actions)


# ── переезд кнопкой ──────────────────────────────────────────────────────────

@router.callback_query(RsvCB.filter(F.action == "move"))
async def move_ask(cb: CallbackQuery, services, **_):
    if config.GUEST:
        await cb.answer()
        return
    why = await call(services.reserve_move_check)
    if why:
        await cb.answer(ui.toast_cap(why), show_alert=True)
        return
    d = await call(services.reserve_move_dialog_data)
    await edit(cb, st_t.move_dialog(d), rkb.confirm_move())
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "move_yes"))
async def move_go(cb: CallbackQuery, services, **_):
    try:
        await call(services.reserve_move_start, cb.message.chat.id, cb.message.message_id)
    except ServiceError as e:
        await cb.answer(ui.toast_cap(e, html=getattr(e, "html", False)), show_alert=True)
        return
    await edit(cb, st_t.move_live(await call(services.reserve_move_live)), None)
    await cb.answer("Перевод начат")


async def refresh_live(bot, services) -> None:
    """Такт автомата: перерисовать живое сообщение хода, если оно есть."""
    d = await call(services.reserve_move_live)
    msg = d.get("msg") or []
    if len(msg) != 2:
        return
    done = d.get("state") in ("away", "home")
    try:
        # итог — с кнопкой назад в раздел: сообщение хода было живым меню, без кнопок админ в тупике
        await bot.edit_message_text(st_t.move_live(d), chat_id=int(msg[0]), message_id=int(msg[1]),
                                    reply_markup=rkb.after_move() if done else None)
    except Exception as e:                                # noqa: BLE001 — не изменилось, удалено
        if "not modified" not in str(e):
            log.info("резерв: живое сообщение хода не поправлено: %s", e)
    if done:
        # ход закончен — сообщение больше не правим
        await call(services.rsv_update, "rsv_out", msg=[])


# ── возврат (основа, рычаг «по кнопке») ──────────────────────────────────────

async def back_ask(cb: CallbackQuery, services, **_):
    """«↩️ Вернуть пользователей» у основы (рычаг «по кнопке»): диалог Т-Р11б. Зовётся
    из reserve/guest.py — там единственный хендлер этого действия."""
    why = await call(services.reserve_return_check)
    if why:
        await cb.answer(ui.toast_cap(why), show_alert=True)
        return
    d = await call(services.reserve_return_dialog_data)
    await edit(cb, st_t.return_dialog(d), rkb.confirm_return())
    await cb.answer()


async def back_go(cb: CallbackQuery, services, **_):
    try:
        actions = await call(services.reserve_return_start, "button")
    except ServiceError as e:
        await cb.answer(ui.toast_cap(e, html=getattr(e, "html", False)), show_alert=True)
        return
    await _run(actions)
    await _render(cb, "rsv", services)
    await cb.answer("Возврат начат")


# ── вернуть клиентов к основе (резерв) ───────────────────────────────────────

@router.callback_query(RsvCB.filter(F.action == "force"))
async def force_ask(cb: CallbackQuery, services, **_):
    """С9: резерв не на связи — вернуть пользователей без него (диалог)."""
    why = await call(services.reserve_force_check)
    if why:
        await cb.answer(ui.toast_cap(why), show_alert=True)
        return
    d = await call(services.reserve_force_dialog_data)
    await edit(cb, st_t.force_dialog(d), rkb.confirm_force())
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "force_yes"))
async def force_go(cb: CallbackQuery, services, **_):
    try:
        actions = await call(services.reserve_force_home)
    except ServiceError as e:
        await cb.answer(ui.toast_cap(e, html=getattr(e, "html", False)), show_alert=True)
        return
    await _run(actions)
    await _render(cb, "rsv", services)
    await cb.answer("Запись возвращается, Telegram забираю")


@router.callback_query(RsvCB.filter(F.action == "evict"))
async def evict_ask(cb: CallbackQuery, services, **_):
    why = await call(services.reserve_evict_check)
    if why:
        await cb.answer(ui.toast_cap(why), show_alert=True)
        return
    await edit(cb, st_t.evict_dialog(await call(services.reserve_peer_name)), rkb.confirm_evict())
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "evict_yes"))
async def evict_go(cb: CallbackQuery, services, **_):
    why = await call(services.reserve_evict_check)
    if why:
        await cb.answer(ui.toast_cap(why), show_alert=True)
        return
    await _run([("send", "evict", {})])
    await _render(cb, "rsv", services)
    await cb.answer("Просьба о возврате отправлена")
