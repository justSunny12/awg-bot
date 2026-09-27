"""
handlers/admin/selfops.py — личный VPN администратора (AdminSelfCB): своё
устройство (только имя) и выдача с выбором устройства.
"""

from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import AdminSelfCB, Menu
from awgbot.bot.handlers.common import call, edit, ask_here, ask_tracked, back_to_context, cleanup_content, send_menu
from awgbot.domain.services import LimitReached, ServiceError
from awgbot.bot.states import AdminSelfAddDevice

router = Router(name="admin.selfops")


async def _self(services):
    ac = await call(services.admin_client)
    if ac is None:
        await call(services.ensure_admin_client)
        ac = await call(services.admin_client)
    return ac


@router.callback_query(AdminSelfCB.filter(F.action == "devices"))
async def self_devices(cb: CallbackQuery, services, state: FSMContext):
    from awgbot.bot.handlers.admin.devices import admin_menu_devices
    await admin_menu_devices(cb, services, state)


@router.callback_query(AdminSelfCB.filter(F.action.in_(kb.GEN_ACTIONS)))
async def self_gen_pick(cb: CallbackQuery, callback_data: AdminSelfCB, services):
    """Кнопка старого образца — выбор своего устройства под выдачу."""
    ac = await _self(services)
    devices = kb.issuable(await call(services.db.list_devices, ac.id))
    if not devices:
        await cb.answer("Сначала добавь устройство", show_alert=True)
        return
    from awgbot.bot import paging
    await edit(cb, kb.PICK_DEVICE_PROMPT[callback_data.action],
               kb.pick_device(devices, callback_data.action, render=cb.data,
                              back_cb=Menu(action="devices").pack(),
                              page=paging.page_of(cb.message.chat.id, "pick")))
    await cb.answer()


@router.callback_query(AdminSelfCB.filter(F.action == "add"))
async def self_add_start(cb: CallbackQuery, services, state: FSMContext):
    ac = await _self(services)
    used, limit = await call(services.device_slots, ac.id)
    if limit != 0 and used >= limit:
        await cb.answer(texts.limit_exhausted_line(used, limit), show_alert=True)
        return
    await state.set_state(AdminSelfAddDevice.name)
    await ask_here(cb, services, state, texts.add_device_prompt(used, limit, for_friend=False), "devices")
    await cb.answer()


@router.message(AdminSelfAddDevice.name)
async def self_add_name(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    await state.clear()
    ac = await _self(services)
    try:
        created = await call(services.add_device, ac.id, name, 0)
    except (LimitReached, ServiceError) as e:
        await back_to_context(message, services, {"ctx_kind": "devices"}, "admin", note=f"⚠️ {texts._e(str(e))}")
        return
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, texts.device_created(name, 0), kb.device_created_kb(created.device_id))
