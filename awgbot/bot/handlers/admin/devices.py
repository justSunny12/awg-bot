"""
handlers/admin/devices.py — устройства глазами администратора.

«📱 Мои устройства», устройство профилю (только имя), карточка устройства с
рядом выдачи, лимит пресетами, перенос в другой профиль, переименование,
удаление, устройства без профиля.
"""

from __future__ import annotations

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import ClientCB, DelDeviceCB, DeviceCB, Menu, PresetCB, ReassignCB
from awgbot.bot.handlers.common import (call, edit, edit_nav, ask_here, ask_tracked, back_to_context,
                                        cleanup_content, drop_message, remove_device_and_notify,
                                        send_device_config)
from awgbot.bot.notifier import notify_one
from awgbot.domain.services import LimitReached, ServiceError
from awgbot.bot.states import AdminAddDevice, EditDeviceName, EditTrafficLimit
from awgbot.bot.handlers.admin.panel import _return_panel, _bot
from awgbot.bot.handlers import devcore

router = Router(name="admin.devices")


# ─────────────────────────────────────────────────────────────────────────────
# Мои устройства (админ — такой же пользователь VPN)
# ─────────────────────────────────────────────────────────────────────────────

async def my_devices_parts(services, chat_id: int = 0):
    ac = await call(services.admin_client)
    if ac is None:
        await call(services.ensure_admin_client)
        ac = await call(services.admin_client)
    devices = await call(services.db.list_devices, ac.id)
    used, limit = await call(services.device_quota, ac.id)
    from awgbot.bot import paging
    return (texts.my_devices_header(len(devices), limit),
            kb.admin_devices(devices, page=paging.page_of(chat_id, "devices"),
                             can_add=not limit or used < limit))


@router.callback_query(Menu.filter(F.action == "devices"))
async def admin_menu_devices(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await edit(cb, *await my_devices_parts(services, cb.message.chat.id))
    await cb.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Устройство профилю: одно имя, лимит — в карточке
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "add_device"))
async def admin_add_device_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await call(services.db.get_client, callback_data.client_id)
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    used, limit = await call(services.device_quota, client.id)
    if limit != 0 and used >= limit:              # 0 = безлимит
        # как при переносе: предложить слот, а не отказать
        await edit(cb, texts.reassign_slot_ask(client, _bot(services), used=used), kb.add_device_addslot(client.id))
        await cb.answer()
        return
    await state.set_state(AdminAddDevice.name)
    await ask_here(cb, services, state, texts.add_device_prompt_admin(client, used, limit),
                   "cl", client.id, client_id=client.id)
    await cb.answer()


@router.callback_query(ClientCB.filter(F.action == "add_device_slot"))
async def admin_add_device_slot(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    """«➕ Слот и добавить»: лимит профиля +1, затем ввод имени."""
    client = await call(services.db.get_client, callback_data.client_id)
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    used, limit = await call(services.device_quota, client.id)
    if limit != 0 and used >= limit:
        await call(services.set_device_limit, client.id, used + 1)
        if client.tg_id and client.tg_id != config.ADMIN_ID:
            await notify_one(cb.bot, client.tg_id, texts.limit_changed_notice(limit, used + 1))
        limit = used + 1
    await state.set_state(AdminAddDevice.name)
    await ask_here(cb, services, state, texts.add_device_prompt_admin(client, used, limit),
                   "cl", client.id, client_id=client.id)
    await cb.answer()


@router.message(AdminAddDevice.name)
async def admin_add_device_name(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    data = await state.get_data()
    await state.clear()
    client = await call(services.db.get_client, data.get("client_id"))
    if client is None:
        await back_to_context(message, services, {}, "admin")
        return
    try:
        created = await call(services.add_device, client.id, name, 0)
    except LimitReached:
        used, limit = await call(services.device_quota, client.id)
        await back_to_context(message, services, data, "admin", note="⚠️ " + texts.limit_reached_line(used, limit))
        return
    except ServiceError as e:
        await back_to_context(message, services, data, "admin", note=f"⚠️ {texts._e(str(e))}")
        return
    # клиенту — уведомление с рядом выдачи; себе админ добавляет иначе
    if client.tg_id and client.tg_id != config.ADMIN_ID:
        used, limit = await call(services.device_quota, client.id)
        await notify_one(message.bot, client.tg_id,
                         texts.reassign_recipient_notice(name, used, limit),
                         reply_markup=kb.added_by_admin(created.device_id))
    await back_to_context(message, services, data, "admin",
                          note=texts.device_created_admin(name, client, _bot(services)))


# ─────────────────────────────────────────────────────────────────────────────
# Карточка устройства, выдача
# ─────────────────────────────────────────────────────────────────────────────

async def _back_target(services, dev) -> str:
    """«Назад» — из принадлежности устройства: без профиля, свои, профиль."""
    service_id = await call(services.db.get_service_client_id)
    if dev.client_id == service_id:
        return Menu(action="unassigned").pack()
    admin_own = await call(services.admin_client)
    if admin_own and dev.client_id == admin_own.id:
        return Menu(action="devices").pack()
    return ClientCB(action="open", client_id=dev.client_id).pack()


async def device_card_parts(services, dev):
    """(текст, клавиатура) карточки устройства для админа; шлюз — карточка
    слота."""
    if dev.is_gateway:
        gw = await call(services.db.gateway_by_device, dev.id)
        slot = gw.id if gw is not None else 0
        if slot:
            from awgbot.bot.handlers.admin.panel import gateway_card_screen
            return await gateway_card_screen(services, slot)
        return texts.gateway_device_card(dev, None), kb.gateway_card_button(0)
    client = await call(services.db.get_client, dev.client_id)
    plimit = int(client.traffic_limit) if client else 0
    service_id = await call(services.db.get_service_client_id)
    text = texts.admin_device_card(dev, client if dev.client_id != service_id else None,
                                   rf=await call(services.rf_device_card, dev),
                                   profile_limit_bytes=plimit, bot_username=_bot(services))
    return text, kb.device_actions(dev, is_admin=True, back_target=await _back_target(services, dev))


@router.callback_query(DeviceCB.filter(F.action == "open"))
async def admin_device_open(cb: CallbackQuery, callback_data: DeviceCB, services, state: FSMContext):
    await state.clear()
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, *await device_card_parts(services, dev))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "connect_menu"))
async def admin_device_connect_menu(cb: CallbackQuery, callback_data: DeviceCB, services, state: FSMContext):
    """Кнопка старого образца — карточка с рядом выдачи."""
    await admin_device_open(cb, callback_data, services, state)


@router.callback_query(DeviceCB.filter(F.action.in_(kb.GEN_ACTIONS)))
async def admin_dev_gen(cb: CallbackQuery, callback_data: DeviceCB, services):
    """Ссылка / QR / файл любого устройства — одним сообщением с «⬅️ В меню»."""
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if not dev.private_key:
        await edit(cb, texts.UNMANAGED_DEVICE_DIALOG, kb.unmanaged_device_dialog(dev.id))
        await cb.answer()
        return
    await drop_message(cb)
    try:
        await send_device_config(cb.message, services, dev, kb.gen_kind(callback_data.action),
                                 finisher=kb.to_menu())
    except ServiceError as e:
        await cb.message.answer(f"⚠️ {texts._e(str(e))}")
        await _return_panel(cb.message, services)
    await cb.answer()


@router.callback_query(ClientCB.filter(F.action == "devices"))
async def admin_client_devices(cb: CallbackQuery, callback_data: ClientCB, services):
    """Устройства профиля отдельным экраном — когда в карточку не влезли."""
    client = await call(services.db.get_client, callback_data.client_id)
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    devices = await call(services.db.list_devices, client.id)
    used, limit = await call(services.device_quota, client.id)
    from awgbot.bot import paging
    await edit(cb, texts.client_devices_header(client, used, limit),
               kb.admin_client_device_list(devices, client.id,
                                           page=paging.page_of(cb.message.chat.id, "clidevs", client.id)))
    await cb.answer()


@router.callback_query(ClientCB.filter(F.action == "gen_for"))
async def admin_gen_for(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    """Кнопка старого образца «Выдать конфиг» — карточка профиля."""
    from awgbot.bot.handlers.admin.clients import client_open
    await client_open(cb, callback_data, services, state)


# ─────────────────────────────────────────────────────────────────────────────
# Имя и лимит устройства
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(DeviceCB.filter(F.action == "edit_name"))
async def device_edit_name_start(cb: CallbackQuery, callback_data: DeviceCB, services, state: FSMContext):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await state.set_state(EditDeviceName.value)
    await ask_here(cb, services, state, texts.device_name_prompt(dev.name), "dev", dev.id, device_id=dev.id)
    await cb.answer()


@router.message(EditDeviceName.value)
async def device_edit_name_apply(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    data = await state.get_data()
    await state.clear()
    old_dev = await call(services.db.get_device, data.get("device_id"))
    if old_dev is None:
        await back_to_context(message, services, {}, "admin")
        return
    try:
        await call(services.rename_device, old_dev.id, name)
    except ServiceError as e:
        await back_to_context(message, services, data, "admin", note=f"⚠️ {texts._e(str(e))}")
        return
    await back_to_context(message, services, data, "admin", note=texts.name_note(old_dev.name, name))


@router.callback_query(DeviceCB.filter(F.action == "edit_traffic"))
async def edit_device_traffic_start(cb: CallbackQuery, callback_data: DeviceCB, services, state: FSMContext):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await state.clear()
    plimit = await call(services.profile_traffic_limit, dev.client_id)
    await edit(cb, texts.device_limit_prompt(dev.name, plimit),
               kb.device_limit_kb(dev.id, plimit, DeviceCB(action="open", device_id=dev.id)))
    await cb.answer()


@router.callback_query(PresetCB.filter(F.kind == "devlimit"))
async def device_limit_preset(cb: CallbackQuery, callback_data: PresetCB, services, state: FSMContext):
    dev = await call(services.db.get_device, callback_data.ref)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if callback_data.val < 0:
        plimit = await call(services.profile_traffic_limit, dev.client_id)
        await state.set_state(EditTrafficLimit.value)
        await ask_here(cb, services, state, texts.device_limit_other_prompt(plimit), "dev", dev.id,
                       target="device", dev_ref=dev.id)
        await cb.answer()
        return
    note, ok = await devcore.apply_device_limit(services, dev, int(callback_data.val))
    if not ok:
        await cb.answer(note, show_alert=True)
        return
    from awgbot.bot import screens
    fresh = await call(services.db.get_device, dev.id)
    text, markup = await device_card_parts(services, fresh)
    await edit(cb, screens.with_note(text, note), markup)
    await cb.answer()


async def apply_device_limit_typed(message: Message, services, state: FSMContext, data: dict,
                                   gb_value: int) -> None:
    """Ввод «✏️ Другое» для лимита устройства (общий приёмник — в clients.py)."""
    dev = await call(services.db.get_device, data.get("dev_ref"))
    if dev is None:
        await state.clear()
        await back_to_context(message, services, {}, "admin")
        return
    note, ok = await devcore.apply_device_limit(services, dev, gb_value)
    if not ok:
        await ask_tracked(message, services, note)
        return
    await state.clear()
    await back_to_context(message, services, data, "admin", note=note)


# ─────────────────────────────────────────────────────────────────────────────
# Перенос в другой профиль
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(DeviceCB.filter(F.action == "reassign"))
async def device_reassign_start(cb: CallbackQuery, callback_data: DeviceCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    clients = [c for c in await call(services.db.list_clients) if c.id != dev.client_id]
    if not clients:
        await cb.answer("Нет других профилей", show_alert=True)
        return
    from awgbot.bot import paging
    owner = await call(services.db.get_client, dev.client_id)
    await edit(cb, texts.reassign_ask(dev, None if owner is None or owner.is_service else owner, _bot(services)),
               kb.reassign_targets(dev.id, clients,
                                   page=paging.page_of(cb.message.chat.id, "reassign", dev.id)))
    await cb.answer()


@router.callback_query(ReassignCB.filter(F.stage == "go"))
async def device_reassign_apply(cb: CallbackQuery, callback_data: ReassignCB, services):
    """Есть место — сразу; иначе вопрос про слот."""
    if not await call(services.has_free_slot, callback_data.client_id):
        client = await call(services.db.get_client, callback_data.client_id)
        if client is None:
            await cb.answer("Профиль не найден", show_alert=True)
            return
        await edit(cb, texts.reassign_slot_ask(client, _bot(services)),
                   kb.reassign_addslot(callback_data.device_id, callback_data.client_id))
        await cb.answer()
        return
    await _do_reassign(cb, services, callback_data.device_id, callback_data.client_id, add_slot=False)


@router.callback_query(ReassignCB.filter(F.stage == "slot_yes"))
async def device_reassign_slot_yes(cb: CallbackQuery, callback_data: ReassignCB, services):
    await _do_reassign(cb, services, callback_data.device_id, callback_data.client_id, add_slot=True)


@router.callback_query(ReassignCB.filter(F.stage == "slot_no"))
async def device_reassign_slot_no(cb: CallbackQuery, callback_data: ReassignCB, services, state: FSMContext):
    await admin_device_open(cb, DeviceCB(action="open", device_id=callback_data.device_id), services, state)


async def _do_reassign(cb, services, device_id, client_id, *, add_slot: bool):
    before = await call(services.db.get_device, device_id)
    donor_client = await call(services.db.get_client, before.client_id) if before is not None else None
    try:
        info = await call(services.reassign_device, device_id, client_id, add_slot)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    rec = info["recipient"]
    if rec["tg_id"]:
        is_admin_rec = rec["tg_id"] == config.ADMIN_ID
        if info["added_slot"]:
            note = texts.reassign_recipient_notice_with_slot(info["name"], rec["count"], rec["limit"],
                                                             recipient_is_admin=is_admin_rec)
        else:
            note = texts.reassign_recipient_notice(info["name"], rec["count"], rec["limit"],
                                                   recipient_is_admin=is_admin_rec)
        await notify_one(cb.bot, rec["tg_id"], note)
    donor = info["donor"]
    if donor and donor["tg_id"]:
        await notify_one(cb.bot, donor["tg_id"],
                         texts.reassign_donor_notice(info["name"], donor["count"], donor["limit"]))
    if info.get("holder_tg"):
        await notify_one(cb.bot, info["holder_tg"], texts.lent_device_reassigned_notice(info["name"]))
    await cb.answer("Перенесено")
    # итог первой строкой карточки устройства на новом месте
    from awgbot.bot import screens
    dev = await call(services.db.get_device, device_id)
    client = await call(services.db.get_client, client_id)
    note = texts.reassigned_note(info["name"], client, _bot(services), donor=donor_client)
    if dev is None:
        await edit_nav(cb, services, note, kb.to_menu())
        return
    text, markup = await device_card_parts(services, dev)
    await edit(cb, screens.with_note(text, note), markup)


# ─────────────────────────────────────────────────────────────────────────────
# Удаление
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(DelDeviceCB.filter(F.stage == "ask"))
async def admin_del_ask(cb: CallbackQuery, callback_data: DelDeviceCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    client = await call(services.db.get_client, dev.client_id)
    only = await call(services.is_only_device, dev.id)
    await edit(cb, texts.admin_device_delete_ask(dev, client, only=only, bot_username=_bot(services)),
               kb.confirm_delete_device(dev.id))
    await cb.answer()


@router.callback_query(DelDeviceCB.filter(F.stage == "confirm"))
async def admin_del_confirm(cb: CallbackQuery, callback_data: DelDeviceCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    client = await call(services.db.get_client, dev.client_id)
    back = await _back_target(services, dev)
    try:
        await remove_device_and_notify(cb.bot, services, dev.id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer()
    # итог — первой строкой экрана, откуда пришли (как у владельца и гостя):
    # свои устройства, профиль или «без профиля»
    from awgbot.bot import screens
    note = texts.device_deleted_note(dev, client, _bot(services))
    kind, ref = ("devices", 0)
    if back.startswith(Menu.__prefix__ + ":unassigned"):
        kind = "unassigned"
    elif back.startswith(ClientCB.__prefix__ + ":"):
        kind, ref = "cl", dev.client_id
    parts = await screens.render(kind, ref, services=services, role="admin", chat_id=cb.message.chat.id)
    if parts is None:
        await edit(cb, note, None)
        await _return_panel(cb.message, services, keep_id=cb.message.message_id)
        return
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    text, markup = parts
    await edit(cb, screens.with_note(text, note), markup)
