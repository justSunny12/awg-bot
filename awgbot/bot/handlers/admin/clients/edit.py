"""edit.py — часть пакета clients (см. __init__): имя, лимиты устройств и трафика, период вручную."""
from __future__ import annotations

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.util import timeutil
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot.callbacks import ClientCB, PresetCB
from awgbot.bot.handlers.common import call, edit, ask_here, ask_tracked, back_to_context
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.domain.services import BYTES_PER_GB, ServiceError
from awgbot.bot.states import EditLimit, EditName, EditPeriod, EditTrafficLimit
from .card import router, client_edit_parts


# ─────────────────────────────────────────────────────────────────────────────
# Имя / лимиты / период — с возвратом в «✏️ Изменить»
# ─────────────────────────────────────────────────────────────────────────────

async def _client_or_alert(cb: CallbackQuery, services, client_id: int):
    client = await call(services.db.get_client, client_id)
    if client is None:
        await cb.answer(ui.Toast.no_profile, show_alert=True)
    return client


@router.callback_query(ClientCB.filter(F.action == "edit_name"))
async def edit_name_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    is_admin = client.tg_id == config.ADMIN_ID
    await state.set_state(EditName.value)
    await ask_here(cb, services, state, texts.client_name_prompt(client),
                   "cl" if is_admin else "edit", client.id, client_id=client.id)
    await cb.answer()


@router.message(EditName.value)
async def edit_name_apply(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    data = await state.get_data()
    await state.clear()
    old = await call(services.db.get_client, data.get("client_id"))
    if old is None:
        await back_to_context(message, services, {}, "admin")
        return
    await call(services.db.update_client_fields, old.id, name=name)
    await back_to_context(message, services, data, "admin", note=texts.client_name_note(old.name, name))


@router.callback_query(ClientCB.filter(F.action == "edit_limit"))
async def edit_limit_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    await state.clear()
    used = await call(services.db.count_devices, client.id)
    await edit(cb, texts.devs_limit_prompt(client, used), kb.devs_limit_kb(client.id))
    await cb.answer()


async def _apply_devs_limit(cb_or_msg, services, client, new_limit: int, *, via_cb: CallbackQuery | None,
                            data: dict | None = None) -> None:
    """Применить лимит устройств: без подтверждения, итог первой строкой
    «✏️ Изменить», клиенту — уведомление."""
    old_limit = int(client.device_limit)
    used = await call(services.set_device_limit, client.id, new_limit)
    note = texts.devs_limit_note(old_limit, new_limit, used)
    if client.tg_id and old_limit != new_limit and client.tg_id != config.ADMIN_ID:
        await notify_one(cb_or_msg.bot, client.tg_id, texts.limit_changed_notice(old_limit, new_limit))
    if via_cb is not None:
        from awgbot.bot import screens
        text, markup = await client_edit_parts(services, client.id)
        await edit(via_cb, screens.with_note(text, note), markup)
        await via_cb.answer()
        return
    await back_to_context(cb_or_msg, services, data or {"ctx_kind": "edit", "ctx_ref": client.id},
                          "admin", note=note)


@router.callback_query(PresetCB.filter(F.kind == "cli_devs"))
async def edit_limit_preset(cb: CallbackQuery, callback_data: PresetCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.ref)
    if client is None:
        return
    if callback_data.val < 0:
        await state.set_state(EditLimit.value)
        await ask_here(cb, services, state, texts.OTHER_NUMBER_PROMPT, "edit", client.id,
                       client_id=client.id)
        await cb.answer()
        return
    await _apply_devs_limit(cb, services, client, int(callback_data.val), via_cb=cb)


@router.message(EditLimit.value)
async def edit_limit_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD_LIMIT)
        return
    data = await state.get_data()
    await state.clear()
    client = await call(services.db.get_client, data.get("client_id"))
    if client is None:
        await back_to_context(message, services, {}, "admin")
        return
    await _apply_devs_limit(message, services, client, int(raw), via_cb=None, data=data)


@router.callback_query(ClientCB.filter(F.action == "edit_traffic"))
async def edit_client_traffic_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    await state.clear()
    await edit(cb, texts.traffic_limit_prompt(client), kb.traffic_limit_kb(client.id))
    await cb.answer()


async def _apply_traffic_limit(cb_or_msg, services, client, gb_value: int, *, via_cb: CallbackQuery | None,
                               data: dict | None = None) -> None:
    old_b = int(client.traffic_limit)
    new_b = gb_value * BYTES_PER_GB
    await call(services.set_client_traffic_limit, client.id, new_b)
    note = texts.traffic_limit_note(old_b, new_b)
    if via_cb is not None:
        from awgbot.bot import screens
        text, markup = await client_edit_parts(services, client.id)
        await edit(via_cb, screens.with_note(text, note), markup)
        await via_cb.answer()
        return
    await back_to_context(cb_or_msg, services, data or {"ctx_kind": "edit", "ctx_ref": client.id},
                          "admin", note=note)


@router.callback_query(PresetCB.filter(F.kind == "cli_traffic"))
async def edit_traffic_preset(cb: CallbackQuery, callback_data: PresetCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.ref)
    if client is None:
        return
    if callback_data.val < 0:
        await state.set_state(EditTrafficLimit.value)
        await ask_here(cb, services, state, texts.OTHER_NUMBER_PROMPT, "edit", client.id,
                       target="client", client_id=client.id)
        await cb.answer()
        return
    await _apply_traffic_limit(cb, services, client, int(callback_data.val), via_cb=cb)


@router.message(EditTrafficLimit.value)
async def edit_traffic_apply(message: Message, services, state: FSMContext):
    """Ввод лимита профиля или устройства («✏️ Другое»)."""
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD_LIMIT)
        return
    data = await state.get_data()
    if data.get("target") == "device":
        from awgbot.bot.handlers.admin.devices import apply_device_limit_typed
        await apply_device_limit_typed(message, services, state, data, int(raw))
        return
    await state.clear()
    client = await call(services.db.get_client, data.get("client_id"))
    if client is None:
        await back_to_context(message, services, {}, "admin")
        return
    await _apply_traffic_limit(message, services, client, int(raw), via_cb=None, data=data)


# ── период вручную ───────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "edit_period"))
async def edit_period_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    await state.set_state(EditPeriod.start)
    await ask_here(cb, services, state, texts.period_start_prompt(client), "edit", client.id,
                   client_id=client.id)
    await cb.answer()


@router.message(EditPeriod.start)
async def edit_period_start_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    data = await state.get_data()
    client = await call(services.db.get_client, data.get("client_id"))
    if client is None:
        await state.clear()
        await back_to_context(message, services, {}, "admin")
        return
    if raw == "-":
        new_start = client.period_start and timeutil.parse_iso(client.period_start)
        if new_start is None:
            await ask_tracked(message, services, texts.PERIOD_NO_START)
            return
    else:
        try:
            new_start = timeutil.parse_dt_sec(raw)
        except ValueError:
            await ask_tracked(message, services, texts.PERIOD_BAD)
            return
    await state.update_data(new_start=timeutil.to_iso(new_start))
    await state.set_state(EditPeriod.end)
    await ask_tracked(message, services, texts.period_end_prompt(client))


@router.message(EditPeriod.end)
async def edit_period_end_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    data = await state.get_data()
    client = await call(services.db.get_client, data.get("client_id"))
    if client is None:
        await state.clear()
        await back_to_context(message, services, {}, "admin")
        return
    if raw == "-":
        new_end = client.period_end and timeutil.parse_iso(client.period_end)
    elif raw == "0":
        new_end = None
    else:
        try:
            new_end = timeutil.parse_dt_sec(raw)
        except ValueError:
            await ask_tracked(message, services, texts.PERIOD_BAD)
            return
    await state.clear()
    saved_start = data.get("new_start")
    new_start = timeutil.parse_iso(saved_start) if saved_start else None
    if new_start is None:
        await back_to_context(message, services, data, "admin", note=texts.PERIOD_NO_START)
        return
    try:
        s, e, notes = await call(services.set_subscription_dates, client.id, new_start, new_end)
    except ServiceError as ex:
        await back_to_context(message, services, data, "admin", note=f"⚠️ {texts._e(str(ex))}")
        return
    await send_notifications(message.bot, notes)
    await back_to_context(message, services, data, "admin", note=texts.period_changed_note(s, e))
