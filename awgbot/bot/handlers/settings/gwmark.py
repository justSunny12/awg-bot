"""gwmark.py — назначение шлюза: выбор устройства, новое устройство, токен агента, файл первого применения."""

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
from awgbot.util import bundlecrypt
from awgbot.bot.handlers.common import call, edit, send_menu, ask_here, ask_tracked, cleanup_content

log = logging.getLogger("awgbot.handlers.settings")
from ._router import router
from .render import _bundle_display, _drop_bundle_msgs

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
    """Существующее устройство — всегда полный путь: ключи линка слота новые
    (устройство линка не знает, а прежняя машина теряет его сама), файл
    первого применения едет открытым и ставится руками, поэтому нужен токен
    агента слота. Та же дорога, что у нового устройства."""
    slot = _slot_of(callback_data)
    # номер нового слота — первый свободный, как его выдаст gateway_setup:
    # «число слотов + 1» после снятия слота 1 при живом 2 давало токен не тому слоту
    try:
        token_slot = slot or (await call(services.gateway_next_slot))[0]
    except ServiceError as e:                      # слоты заняты, нет портов/подсетей
        await cb.answer(ui.toast(e), show_alert=True)
        await state.clear()
        return
    if not await call(services.gw_bot_token, token_slot):
        await cb.answer()
        await state.set_state(GatewayToken.value)
        await state.update_data(gw_device_id=callback_data.device_id, gw_slot=slot)
        await ask_here(cb, services, state, texts.gateway_ask_token(token_slot), "set_rt_gw", slot or 0)
        return
    await cb.answer("Назначаю…")
    await edit(cb, "🛰 Назначаю шлюз…", None)
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
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
    уедет внутрь файла первого применения, и установка на шлюзе не задаст ни
    одного вопроса. Токен уже есть — идём сразу к выпуску."""
    slot = _slot_of(callback_data)
    try:
        token_slot = slot or (await call(services.gateway_next_slot))[0]
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        await state.clear()
        return
    if not await call(services.gw_bot_token, token_slot):
        await cb.answer()
        await state.set_state(GatewayToken.value)
        await state.update_data(gw_slot=slot)
        await ask_here(cb, services, state, texts.gateway_ask_token(token_slot), "set_rt_gw", slot or 0)
        return
    await cb.answer("Создаю устройство и ключи…")
    await edit(cb, "🛰 Создаю устройство и ключи…", None)       # экран выбора отслужил
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
    await _gateway_setup_go(cb.message, services, slot=slot)


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
        token_slot = slot or (await call(services.gateway_next_slot))[0]
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


async def _gateway_setup_go(message: Message, services, device_id: int | None = None,
                            slot: int = 0) -> None:
    """Назначить шлюзом существующее устройство (со сменой ключей) или создать
    устройство «Шлюз»; выпустить файл первого применения и объяснить одну
    команду на устройстве-шлюзе."""
    try:
        if device_id is not None:
            res = await call(services.gateway_setup, device_id, rekey=True, slot_id=slot or None)
        else:
            res = await call(services.gateway_setup, None, slot_id=slot or None)
    except ServiceError as e:
        await send_menu(message, services, f"⚠️ {texts._e(str(e))}", kb.settings_back("rt"))
        return
    instr = await message.answer(texts.gateway_install_instructions(res["device"],
                                                                    services.bundle_name(res["gateway"]),
                                                                    routing_reset=res.get("routing_reset", False)))
    await _send_plain_bundle(message, services, res["gateway"].id, instr.message_id)


async def _send_plain_bundle(message: Message, services, slot: int = 0, instr_id: int | None = None) -> None:
    """Открытый файл первого применения: внутри ключи и токен агента."""
    try:
        blob, name = await call(services.gw_bundle_plain, slot or None)
    except (ServiceError, OSError) as e:
        await message.answer(ui.fail("Файл первого применения не собран", str(e)))
        return
    from aiogram.types import BufferedInputFile
    if slot:
        await _drop_bundle_msgs(message.bot, services, slot)
    display, _bot = await _bundle_display(services, slot or None)
    sent = await message.answer_document(
        BufferedInputFile(blob, filename=name),
        caption=texts.gateway_plain_bundle_caption(display),
        reply_markup=kb.bundle_menu_kb(slot))
    if slot:
        await call(services.gw_bundle_msg_set, slot, message.chat.id, sent.message_id, instr_id,
                   bundlecrypt.fingerprint(blob), True)
