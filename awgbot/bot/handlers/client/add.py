"""add.py — часть пакета client (см. __init__): добавление устройства (себе и другу) и удаление."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.bot import screens
from awgbot.bot.callbacks import CancelCB, DelDeviceCB, DeviceCB, PresetCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.notifier import notify_one
from awgbot.bot.handlers.common import (
    call, cleanup_content, drop_message, edit, ask_here, ask_tracked, back_to_context, mine_or_held,
    remove_device_and_notify, send_menu)
from awgbot.domain.services import BYTES_PER_GB, LimitReached, ServiceError
from awgbot.bot.states import AddDevice
from awgbot.bot.handlers import devcore
from .main import router, ADD_FROM_DEVICES, main_payload
from .menu import devices_payload, _send_invite


# ── добавление устройства ────────────────────────────────────────────────────

@router.callback_query(DeviceCB.filter(F.action == "add"))
async def device_add_start(cb: CallbackQuery, callback_data: DeviceCB, client, services,
                           state: FSMContext):
    """«➕ Устройство»: приглашение ввода имени на месте экрана с
    переключателем «для друга». Кнопки нет, пока лимит исчерпан; кнопка
    старого образца — всплывашка."""
    used, limit = await call(services.device_quota, client.id)
    if limit != 0 and used >= limit:              # 0 = безлимит
        await cb.answer(texts.limit_exhausted_line(used, limit), show_alert=True)
        return
    ctx = "devices" if callback_data.device_id == ADD_FROM_DEVICES else "main"
    await state.set_state(AddDevice.name)
    await state.update_data(for_friend=False, ctx_kind=ctx, ctx_ref=0, used=used, limit=limit)
    await edit(cb, texts.add_device_prompt(used, limit, for_friend=False),
               kb.add_device_kb(for_friend=False, ctx_kind=ctx))
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action.in_(("add_self", "add_friend"))), AddDevice.name)
async def device_add_for_whom(cb: CallbackQuery, callback_data: DeviceCB, client, services,
                              state: FSMContext):
    """Переключатель «для кого» на приглашении: меняет текст и кнопку."""
    for_friend = callback_data.action == "add_friend"
    data = await state.get_data()
    await state.update_data(for_friend=for_friend)
    await edit(cb, texts.add_device_prompt(data.get("used", 0), data.get("limit", 0), for_friend=for_friend),
               kb.add_device_kb(for_friend=for_friend, ctx_kind=data.get("ctx_kind") or "main"))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action.in_(("add_self", "add_friend"))))
async def device_add_for_whom_stale(cb: CallbackQuery, client, services, state: FSMContext):
    """Переключатель без диалога (кнопка старого образца) — начать заново."""
    await device_add_start(cb, DeviceCB(action="add"), client, services, state)


@router.message(AddDevice.name, RoleFilter("client"))
async def device_add_name(message: Message, client, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    data = await state.get_data()
    if data.get("for_friend"):
        # другу — лимит пресетами не выше лимита профиля, затем приглашение
        await state.update_data(dev_name=name)
        await state.set_state(AddDevice.traffic)
        plimit = await call(services.profile_traffic_limit, client.id)
        await cleanup_content(message.bot, services, message.chat.id)
        await send_menu(message, services, texts.device_limit_prompt(name, plimit),
                        kb.client_device_limit_kb(0, plimit, CancelCB(kind=data.get("ctx_kind") or "main")))
        return
    await state.clear()
    await _create_own(message, services, client, name)


async def _limit_note(services, client) -> str:
    """«⚠️ Лимит исчерпан: чтобы добавить новое, удали N» — лимит кончился, пока
    человек вводил имя."""
    used, limit = await call(services.device_quota, client.id)
    return "⚠️ " + (texts.limit_exhausted_line(used, limit) or "Лимит исчерпан")


async def _create_own(message: Message, services, client, name: str) -> None:
    """Своё устройство: без вопроса о лимите (ставится в карточке), сразу
    экран с рядом выдачи."""
    try:
        created = await call(services.add_device, client.id, name, 0)
    except LimitReached:
        await back_to_context(message, services, {}, "client", client, note=await _limit_note(services, client))
        return
    except ServiceError as e:
        await back_to_context(message, services, {}, "client", client, note=f"⚠️ {texts._e(str(e))}")
        return
    plimit = await call(services.profile_traffic_limit, client.id)
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, texts.device_created(name, plimit),
                    kb.device_created_kb(created.device_id))


async def _create_for_friend(target: Message, services, client, name: str, tlimit: int,
                             cb: CallbackQuery | None = None) -> None:
    try:
        created = await call(services.add_device, client.id, name, tlimit)
    except LimitReached:
        await back_to_context(target, services, {}, "client", client, note=await _limit_note(services, client))
        return
    except ServiceError as e:
        await back_to_context(target, services, {}, "client", client, note=f"⚠️ {texts._e(str(e))}")
        return
    code = await call(services.make_device_friendly, created.device_id)
    dev = await call(services.db.get_device, created.device_id)
    await cleanup_content(target.bot, services, target.chat.id)
    await _send_invite(target, services, dev, code)


@router.callback_query(PresetCB.filter((F.kind == "devlimit") & (F.ref == 0)), AddDevice.traffic)
async def device_add_limit_preset(cb: CallbackQuery, callback_data: PresetCB, client, services,
                                  state: FSMContext):
    data = await state.get_data()
    name = data.get("dev_name") or ""
    plimit = await call(services.profile_traffic_limit, client.id)
    if callback_data.val < 0:
        await state.set_state(AddDevice.traffic)
        await ask_here(cb, services, state, texts.device_limit_other_prompt(plimit),
                       data.get("ctx_kind") or "main")
        await cb.answer()
        return
    gb_value = int(callback_data.val)
    if plimit and gb_value * BYTES_PER_GB > plimit:
        await cb.answer(texts.device_limit_over(plimit), show_alert=True)
        return
    await state.clear()
    await drop_message(cb, services)
    await _create_for_friend(cb.message, services, client, name, gb_value * BYTES_PER_GB, cb)
    await cb.answer()


@router.message(AddDevice.traffic, RoleFilter("client"))
async def device_add_traffic(message: Message, client, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD)
        return
    plimit = await call(services.profile_traffic_limit, client.id)
    gb_value = int(raw)
    if plimit and gb_value * BYTES_PER_GB > plimit:
        await ask_tracked(message, services, texts.device_limit_over(plimit))
        return
    data = await state.get_data()
    name = data.get("dev_name")
    await state.clear()
    if not name:
        await back_to_context(message, services, data, "client", client)
        return
    await _create_for_friend(message, services, client, name, gb_value * BYTES_PER_GB)


# ── удаление ─────────────────────────────────────────────────────────────────

async def _show_delete_prompt(cb, services, client, dev):
    """Четыре вопроса: своё / единственное / переданное (у держателя пропадёт
    доступ) / удерживаемое чужое (нового не создать — только код)."""
    if dev.holder_client_id == client.id:
        await edit(cb, texts.device_delete_ask(dev, held=True), kb.confirm_delete_device(dev.id))
        return
    if dev.is_lent:
        await edit(cb, texts.device_delete_ask(dev, lent=True), kb.confirm_delete_device(dev.id))
        return
    only = await call(services.is_only_device, dev.id)
    await edit(cb, texts.device_delete_ask(dev, only=only), kb.confirm_delete_device(dev.id))


@router.callback_query(DelDeviceCB.filter(F.stage == "ask"))
async def device_delete_ask(cb: CallbackQuery, callback_data, client, services):
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await _show_delete_prompt(cb, services, client, dev)
    await cb.answer()


@router.callback_query(DelDeviceCB.filter(F.stage == "confirm"))
async def device_delete_confirm(cb: CallbackQuery, callback_data: DelDeviceCB, client, services):
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    if dev.holder_client_id == client.id:
        if not await devcore.delete_by_holder(cb, services, dev):
            return
        text, markup = await devices_payload(services, client)
        await edit(cb, screens.with_note(text, texts.device_removed(dev.name)), markup)
        return
    try:
        if dev.is_lent:
            await call(services.remove_device, dev.id)
            if dev.holder_tg_id:
                await notify_one(cb.bot, dev.holder_tg_id, texts.lent_device_deleted_by_owner_notice(dev))
        else:
            await remove_device_and_notify(cb.bot, services, dev.id)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    await cb.answer()
    # итог — первой строкой экрана на месте вопроса, как итог создания:
    # «Устройства», а если удалили последнее — главная
    devices = await call(services.db.list_devices, client.id)
    used, limit = await call(services.device_quota, client.id)
    note = texts.device_deleted(dev.name, used, limit)
    if not devices and not await call(services.db.list_held_devices, client.id):
        text, markup = await main_payload(services, client)
    else:
        text, markup = await devices_payload(services, client)
    await edit(cb, screens.with_note(text, note), markup)
