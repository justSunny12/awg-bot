"""accept.py — принятие приглашения (принявший): «📥 Принять приглашение» —
приглашение строки (Т-Р3), проверки (Т-Р4), диалог (Т-Р5) с тумблером
РФ-доступа, «🛟 Подключиться» — линк на ключах приглашения и живое сообщение
с ходом (Т-Р6); код для админа второго сервера правит канал."""
from __future__ import annotations

import logging

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import ui
from awgbot.bot.callbacks import RsvCB
from awgbot.bot.handlers.common import ask_tracked, call, cleanup_content, edit
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.states import ReserveAcceptToken
from awgbot.bot.texts.reserve import pairing as pt
from awgbot.domain.services import ServiceError

from .._router import router

log = logging.getLogger("awgbot.handlers.settings")


@router.callback_query(RsvCB.filter(F.action == "accept"))
async def accept_ask(cb: CallbackQuery, services, state: FSMContext):
    await state.set_state(ReserveAcceptToken.value)
    await ask_tracked(cb.message, services, pt.ACCEPT_PROMPT, reply_markup=rkb.code_prompt())
    await cb.answer()


async def _dialog(services, token: str, rf_on: bool):
    info = await call(services.reserve_accept_parse, token)
    tok = info["tok"]
    has_gw = await call(services.reserve_has_gateway)
    # имя второго сервера приглашение не несёт (оно придёт по каналу) — в диалоге он «второй сервер»
    text = pt.accept_dialog("второй сервер", tok.host, tok.version, rf_via_mine=rf_on, has_gateway=has_gw,
                            warn=info["warn"], ask_version=info["ask_version"])
    return text, rkb.accept_dialog(rf_on, has_gateway=has_gw)


@router.message(ReserveAcceptToken.value, F.text)
async def token_received(message: Message, services, state: FSMContext):
    token = (message.text or "").strip()
    try:
        await message.delete()                            # строка несёт закрытый ключ линка — в чате не держим
    except Exception:                                     # noqa: BLE001
        pass
    try:
        text, markup = await _dialog(services, token, True)
    except ServiceError as e:
        await ask_tracked(message, services, f"⚠️ {ui.toast(e)}", reply_markup=rkb.code_prompt())
        return
    await state.update_data(token=token, rf=True)
    await cleanup_content(message.bot, services, message.chat.id)
    await ask_tracked(message, services, text, reply_markup=markup)


@router.callback_query(RsvCB.filter(F.action == "rf"))
async def rf_toggle(cb: CallbackQuery, services, state: FSMContext):
    data = await state.get_data()
    token = str(data.get("token") or "")
    if not token:
        await cb.answer("Диалог устарел — начни заново", show_alert=True)
        return
    rf_on = not bool(data.get("rf", True))
    await state.update_data(rf=rf_on)
    try:
        text, markup = await _dialog(services, token, rf_on)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    await edit(cb, text, markup)
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "connect"))
async def connect(cb: CallbackQuery, services, state: FSMContext):
    data = await state.get_data()
    token = str(data.get("token") or "")
    if not token:
        await cb.answer("Диалог устарел — начни заново", show_alert=True)
        return
    try:
        st = await call(services.reserve_accept_go, token, bool(data.get("rf", True)))
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    await state.clear()
    await edit(cb, pt.connecting(str(st.get("host") or "")), rkb.accept_message())
    await call(services.reserve_invite_set_message, cb.message.chat.id, cb.message.message_id)
    from awgbot.runtime import pairlink
    try:
        await pairlink.ensure(services)
        pairlink.wake()
    except Exception as e:                                # noqa: BLE001
        log.warning("резерв: клиент канала пары не поднят: %s", e)
    await cb.answer()
