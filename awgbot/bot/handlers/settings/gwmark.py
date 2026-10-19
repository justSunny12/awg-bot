"""gwmark.py — подключение шлюза: выбор устройства, новое устройство, «🔁 Настроить заново», токен агента;
дальше — команда подключения (gwjoin.py)."""

from __future__ import annotations

import logging
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import GwMarkCB
from awgbot.bot.states import GatewayToken
from awgbot.domain.services import ServiceError
from awgbot.bot.handlers.common import call, edit, ask_here, ask_tracked, cleanup_content

log = logging.getLogger("awgbot.handlers.settings")
from ._router import router
from .gwjoin import gwjoin_go

def _slot_of(callback_data) -> int:
    return int(getattr(callback_data, "slot", 0) or 0)


@router.callback_query(GwMarkCB.filter(F.action == "pick_list"))
async def gateway_pick_list(cb: CallbackQuery, callback_data: GwMarkCB, services):
    slot = _slot_of(callback_data)
    cands = await call(services.gateway_candidates)
    await cb.answer()
    if not cands:
        await edit(cb, texts.GATEWAY_PICK_EMPTY, kb.gateway_choose_kind(False, slot))
        return
    from awgbot.bot import paging
    await edit(cb, texts.GATEWAY_PICK_INTRO,
               kb.gateway_pick(cands, slot, page=paging.page_of(cb.message.chat.id, "gwpick", slot)))


@router.callback_query(GwMarkCB.filter(F.action == "pick"))
async def gateway_pick(cb: CallbackQuery, callback_data: GwMarkCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await cb.answer()
    slot = _slot_of(callback_data)
    states = await call(services.gateway_states)
    prev = None
    replace_state = None
    if slot:
        replace_state = next((st for st in states if st["gateway"].id == slot), None)
        prev = replace_state.get("device") if replace_state else None
    await edit(cb, texts.gateway_mark_ask(dev, prev, standby=bool(states) and not slot,
                                          replace_state=replace_state),
               kb.gateway_mark_confirm(dev.id, slot))


@router.callback_query(GwMarkCB.filter(F.action == "mark_yes"))
async def gateway_mark_yes(cb: CallbackQuery, callback_data: GwMarkCB, services,
                           state: FSMContext):
    """Существующее устройство — та же дорога, что у нового: токен бота
    слота (если его ещё нет), затем команда подключения."""
    slot = _slot_of(callback_data)
    # номер нового слота — первый свободный, как его выдаст команда подключения:
    # «число слотов + 1» после снятия слота 1 при живом 2 давало токен не тому слоту
    try:
        token_slot = slot or await call(services.gwjoin_peek_slot)
    except ServiceError as e:                      # слоты заняты, нет портов/подсетей
        await cb.answer(ui.toast(e), show_alert=True)
        await state.clear()
        return
    if not await call(services.gw_bot_token, token_slot):
        await cb.answer()
        await state.set_state(GatewayToken.value)
        await state.update_data(gw_device_id=callback_data.device_id, gw_slot=slot)
        dev = await call(services.db.get_device, callback_data.device_id)
        name = dev.name if dev is not None else texts.gateway_new_name(token_slot)
        await ask_here(cb, services, state, texts.gateway_ask_token(name), "set_rt_gw", slot or 0)
        return
    if not _first_press(cb):
        await cb.answer(ui.toast(JOIN_PREPARING), show_alert=True)   # двойное нажатие — одна команда
        return
    await cb.answer()
    await _screen_done(cb, services)
    await _gateway_setup_go(cb.message, services, callback_data.device_id, slot)


@router.callback_query(GwMarkCB.filter(F.action == "new_ask"))
async def gateway_new_ask(cb: CallbackQuery, callback_data: GwMarkCB, services, state: FSMContext):
    """Новый слот — сразу к выпуску, без подтверждения; замена машины —
    с подтверждением: прежняя потеряет линк."""
    slot = _slot_of(callback_data)
    if not slot:
        await gateway_new_yes(cb, callback_data, services, state)
        return
    await cb.answer()
    st = next((s for s in await call(services.gateway_states) if s["gateway"].id == slot), None)
    prev = st.get("device") if st else None
    await edit(cb, texts.gateway_new_ask(slot, prev_name=prev.name if prev else "",
                                         active=bool(st and st.get("active"))),
               kb.gateway_new_confirm(slot))


@router.callback_query(GwMarkCB.filter(F.action == "new_yes"))
async def gateway_new_yes(cb: CallbackQuery, callback_data: GwMarkCB, services, state: FSMContext):
    """Новая машина. Токен её бота спрашиваем ЗДЕСЬ и один раз на слот: он
    уедет шлюзу каналом после кода, и установка на шлюзе не задаст ни одного
    вопроса. Токен уже есть — идём сразу к команде."""
    slot = _slot_of(callback_data)
    try:
        token_slot = slot or await call(services.gwjoin_peek_slot)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        await state.clear()
        return
    if not await call(services.gw_bot_token, token_slot):
        await cb.answer()
        await state.set_state(GatewayToken.value)
        await state.update_data(gw_slot=slot)
        await ask_here(cb, services, state, texts.gateway_ask_token(texts.gateway_new_name(token_slot)),
                       "set_rt_gw", slot or 0)
        return
    if not _first_press(cb):
        await cb.answer(ui.toast(JOIN_PREPARING), show_alert=True)   # двойное нажатие — одна команда
        return
    await cb.answer()
    await _screen_done(cb, services)
    await _gateway_setup_go(cb.message, services, slot=slot)


@router.callback_query(GwMarkCB.filter(F.action == "same"))
async def gateway_same_ask(cb: CallbackQuery, callback_data: GwMarkCB, services):
    """«🔁 Настроить заново» — то же устройство слота, новая команда."""
    slot = _slot_of(callback_data)
    st = next((s for s in await call(services.gateway_states) if s["gateway"].id == slot), None)
    if st is None:
        await cb.answer(ui.toast("Такого шлюза нет"), show_alert=True)
        return
    await cb.answer()
    await edit(cb, texts.gateway_same_ask(st), kb.gateway_same_confirm(slot))


@router.callback_query(GwMarkCB.filter(F.action == "same_yes"))
async def gateway_same_yes(cb: CallbackQuery, callback_data: GwMarkCB, services):
    if not _first_press(cb):
        await cb.answer(ui.toast(JOIN_PREPARING), show_alert=True)   # двойное нажатие — одна команда
        return
    await cb.answer()
    await _screen_done(cb, services)
    await gwjoin_go(cb.message, services, slot=_slot_of(callback_data), same=True)


@router.message(GatewayToken.value)
async def gateway_token_received(message: Message, state: FSMContext, services):
    token = (message.text or "").strip()
    try:
        await message.delete()          # токен в истории чата не держим — и
    except Exception:                   # noqa: BLE001
        pass                            # непринятый тоже: секрет есть секрет
    data = await state.get_data()
    slot = int(data.get("gw_slot") or 0)
    try:
        token_slot = slot or await call(services.gwjoin_peek_slot)
    except ServiceError as e:
        await state.clear()
        await ask_tracked(message, services, f"⚠️ {texts._e(str(e))}")
        return
    try:
        await call(services.set_gw_bot_token, token, token_slot)
    except ServiceError as e:
        await ask_tracked(message, services, f"⚠️ {texts._e(str(e))}")
        return
    await state.clear()
    await cleanup_content(message.bot, services, message.chat.id)   # приглашение отслужило
    # кто этот бот — сразу: карточка слота ведёт в его чат ссылкой
    from awgbot.runtime import gwbotme
    await gwbotme.refresh(services, token_slot)
    # Токен спрашивают из двух мест: «новая машина» и замена машины со сменой
    # ключей. Куда возвращаться, помнит state.
    device_id = data.get("gw_device_id")
    if device_id:
        await _gateway_setup_go(message, services, int(device_id), slot)
        return
    await _gateway_setup_go(message, services, slot=slot)


JOIN_PREPARING = "Команда подключения уже готовится — она придёт следующим сообщением"
_PRESSED: dict[tuple[int, int], float] = {}


def _first_press(cb: CallbackQuery) -> bool:
    """Первое нажатие «Подключить / Заменить / Настроить заново» под этим
    сообщением (ui-4): второе, пришедшее раньше, чем экран сменился, —
    вторая команда и второй слот. Помним минуту."""
    import time
    now = time.monotonic()
    for k, at in list(_PRESSED.items()):
        if now - at > 60:
            _PRESSED.pop(k, None)
    key = (cb.message.chat.id, cb.message.message_id)
    if key in _PRESSED:
        return False
    _PRESSED[key] = now
    return True


async def _screen_done(cb: CallbackQuery, services) -> None:
    """Экран выбора отслужил: команду несёт отдельное сообщение подключения."""
    await edit(cb, "🛰 Готовлю команду подключения…", None)
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)


async def _gateway_setup_go(message: Message, services, device_id: int | None = None,
                            slot: int = 0) -> None:
    """Своё устройство (device_id) или новое «Шлюз N»; новый слот (slot 0)
    или замена в слоте — команда подключения и сообщение подключения."""
    await gwjoin_go(message, services, device_id=device_id, slot=slot)
