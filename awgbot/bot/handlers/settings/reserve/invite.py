"""invite.py — приглашение в пару (пригласивший): «📤 Пригласить» — строка в
живом сообщении (Т-Р2); «✏️ Ввести код» — приглашение кода (Т-Р7), приём,
попытки; «⬅️ Отменить». Сообщение приглашения — обычное сообщение, не живое
меню: его правит ход сопряжения."""
from __future__ import annotations

import logging

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import ui
from awgbot.bot.callbacks import RsvCB
from awgbot.bot.handlers.common import ask_tracked, call, cleanup_content
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.states import ReserveInviteCode
from awgbot.bot.texts.reserve import pairing as pt
from awgbot.domain.services import ServiceError

from .._router import router
from ..render import _render
from .events import _edit

log = logging.getLogger("awgbot.handlers.settings")


async def _link_ensure(services) -> None:
    from awgbot.runtime import pairlink
    try:
        await pairlink.ensure(services)
    except Exception as e:                                # noqa: BLE001
        log.warning("резерв: канал пары не поднят: %s", e)


@router.callback_query(RsvCB.filter(F.action == "invite"))
async def invite(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    try:
        res = await call(services.reserve_invite_issue)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    st = res["state"]
    if res.get("stale"):
        await _edit(cb.bot, {"msg": res["stale"]}, pt.INVITE_STALE, rkb.final())
    sent = await cb.message.answer(pt.invite_message(st, res["token"]), reply_markup=rkb.invite_message(st["id"]))
    await call(services.reserve_invite_set_message, sent.chat.id, sent.message_id)
    await _link_ensure(services)
    await _render(cb, "rsv", services)
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "code"))
async def code_ask(cb: CallbackQuery, callback_data: RsvCB, services, state: FSMContext):
    res = await call(services.reserve_pair_wait)
    if res == "gone":
        await cb.answer(pt.CODE_GONE, show_alert=True)
        return
    if res == "wait":
        await cb.answer(pt.CODE_WAIT, show_alert=True)
        return
    await state.set_state(ReserveInviteCode.value)
    await state.update_data(rsv_pid=callback_data.pid)
    await ask_tracked(cb.message, services, pt.CODE_PROMPT, reply_markup=rkb.code_prompt())
    await cb.answer()


@router.message(ReserveInviteCode.value, F.text)
async def code_received(message: Message, services, state: FSMContext):
    from awgbot.runtime import pairlink
    res = await call(services.reserve_pair_code, message.text or "")
    r = res.get("result")
    if r == "format":
        await ask_tracked(message, services, pt.CODE_FORMAT, reply_markup=rkb.code_prompt())
        return
    await state.clear()
    await cleanup_content(message.bot, services, message.chat.id)
    st = res.get("st") or {}
    if r == "wait":
        await message.answer(pt.CODE_WAIT)
    elif r in ("gone", "expired"):
        if r == "expired":
            await pairlink.pair_close("expired", notify=True)
        await message.answer(pt.CODE_GONE if r == "gone" else pt.invite_expired(st))
    elif r == "wrong":
        await pairlink.pair_wrong(int(res.get("left") or 0))
        await ask_tracked(message, services, pt.code_wrong(int(res.get("left") or 0)), reply_markup=rkb.code_prompt())
        await state.set_state(ReserveInviteCode.value)
    elif r == "refused":
        await pairlink.pair_close("code")
        await _edit(message.bot, st, pt.CODE_REFUSED, rkb.final())
        await message.answer(pt.CODE_REFUSED)
    elif r == "ok":
        await pairlink.pair_accepted()
        await message.answer("✅ Код верный — подключаю второй сервер, итог будет в сообщении приглашения")


@router.callback_query(RsvCB.filter(F.action == "cancel"))
async def cancel(cb: CallbackQuery, services, state: FSMContext):
    from awgbot.runtime import pairlink
    await state.clear()
    st = await call(services.reserve_invite)
    if not st.get("id"):
        await cb.answer(pt.CODE_GONE, show_alert=True)
        return
    role = st.get("role") or "inviter"
    try:
        if role == "inviter":
            await pairlink.pair_close("cancel")
        else:
            await call(services.reserve_invite_cancel, "cancel")
            await pairlink.ensure(services)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    await _edit(cb.bot, st, pt.INVITE_CANCELLED if role == "inviter" else "ℹ️ Подключение отменено", rkb.final())
    try:
        await _render(cb, "rsv", services)
    except Exception:                                     # noqa: BLE001 — кнопка была на сообщении приглашения, не на меню
        pass
    await cb.answer("Приглашение отменено")
