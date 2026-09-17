"""
handlers/admin/devices.py — устройства глазами администратора.

Добавление устройства профилю, устройства профиля и выдача конфигов, устройства
без профиля, карточка устройства, перепривязка, переименование, удаление.
"""

from __future__ import annotations

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import ClientCB, DelDeviceCB, DeviceCB, Menu, ReassignCB
from awgbot.bot.handlers.common import (call, edit, edit_nav, ask_tracked, drop_message,
                                        remove_device_and_notify, send_device_config,
                                        content_finisher)
from awgbot.bot.notifier import notify_one
from awgbot.domain.services import BYTES_PER_GB, ServiceError
from awgbot.bot.states import AdminAddDevice, EditDeviceName
from awgbot.bot.handlers.admin.panel import _main_menu_markup, _return_panel

router = Router(name="admin.devices")


@router.callback_query(ClientCB.filter(F.action == "add_device"))
async def admin_add_device_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await call(services.db.get_client, callback_data.client_id)
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    used, limit = await call(services.device_slots, callback_data.client_id)
    if limit != 0 and used >= limit:              # 0 = безлимит
        await cb.answer(f"У профиля исчерпан лимит ({used} из {limit})", show_alert=True)
        return
    await state.set_state(AdminAddDevice.name)
    await state.update_data(client_id=callback_data.client_id)
    await ask_tracked(cb.message, services, f"Введи имя устройства для профиля «{client.name}»:", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(AdminAddDevice.name)
async def admin_add_device_name(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, "Имя не может быть пустым. Введи ещё раз:")
        return
    await state.update_data(dev_name=name)
    await state.set_state(AdminAddDevice.traffic)
    data = await state.get_data()
    plimit = await call(services.profile_traffic_limit, data["client_id"])
    await ask_tracked(message, services, texts.traffic_limit_device_ask(plimit), reply_markup=kb.reply_cancel())


@router.message(AdminAddDevice.traffic)
async def admin_add_device_traffic(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.TRAFFIC_LIMIT_BAD)
        return
    data = await state.get_data()
    name = data.get("dev_name")
    client_id = data["client_id"]
    tlimit = int(raw) * BYTES_PER_GB
    await state.clear()
    try:
        created = await call(services.add_device, client_id, name, tlimit)
    except ServiceError as e:
        await message.answer(f"Не удалось создать устройство: {e}", reply_markup=kb.reply_hide())
        await _return_panel(message, services)
        return
    client = await call(services.db.get_client, client_id)
    dev_count = len(await call(services.db.list_devices, client_id)) if client else 0
    plimit = await call(services.profile_traffic_limit, client_id) if client else 0
    await message.answer(
        texts.device_created_report(name, client_name=client.name if client else None,
                                    device_count=dev_count,
                                    max_devices=client.device_limit if client else 0,
                                    dev_limit_bytes=tlimit, profile_limit_bytes=plimit),
        reply_markup=kb.reply_hide())
    # уведомляем клиента — тем же принципом, что при переназначении устройства.
    # Себе админ устройство добавляет другим путём (AdminSelfCB), уведомлять
    # его о собственном действии незачем.
    if client and client.tg_id and client.tg_id != config.ADMIN_ID:
        used, limit = await call(services.device_slots, client_id)
        await notify_one(
            message.bot, client.tg_id,
            texts.reassign_recipient_notice(name, used, limit),
            reply_markup=kb.added_by_admin(created.device_id))
    await _return_panel(message, services)


# ─────────────────────────────────────────────────────────────────────────────
# Выдача конфига клиента (админ) — выбор устройства
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "gen_for"))
async def admin_gen_for(cb: CallbackQuery, callback_data: ClientCB, services):
    devices = kb.issuable(await call(services.db.list_devices, callback_data.client_id))
    if not devices:
        await cb.answer("У профиля нет устройств", show_alert=True)
        return
    back = ClientCB(action="open", client_id=callback_data.client_id).pack()
    await edit(cb, "Выбери устройство:", kb.pick_device(devices, "gen_link", back_cb=back))
    await cb.answer()


@router.callback_query(ClientCB.filter(F.action == "devices"))
async def admin_client_devices(cb: CallbackQuery, callback_data: ClientCB, services):
    devices = await call(services.db.list_devices, callback_data.client_id)
    if not devices:
        markup = kb.admin_client_device_list([], callback_data.client_id)
        await edit(cb, "У этого профиля нет устройств.", markup)
        await cb.answer()
        return
    lines = "\n".join(texts.device_line(d) for d in devices)
    await edit(cb, f"📋 Устройства профиля:\n{lines}",
               kb.admin_client_device_list(devices, callback_data.client_id))
    await cb.answer()


# админ генерирует ссылку/QR/файл для любого устройства (без проверки владения)
@router.callback_query(DeviceCB.filter(F.action.in_(kb.GEN_ACTIONS)))
async def admin_dev_gen(cb: CallbackQuery, callback_data: DeviceCB, services):
    """Один обработчик на три вида выдачи: раньше их было три одинаковых."""
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    kind = kb.gen_kind(callback_data.action)
    await drop_message(cb)                           # убрать «Как подключить» — не висеть над ссылкой
    try:
        await send_device_config(cb.message, services, dev, kind)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        await _return_panel(cb.message, services)
        return
    await content_finisher(cb.message, services, texts.finish_config(kind, dev.name), "admin")
    await cb.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Устройства без клиента → открыть, привязать, реставрировать
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "unassigned"))
async def unassigned_list(cb: CallbackQuery, services):
    service_id = await call(services.db.get_service_client_id)
    devices = await call(services.db.list_devices, service_id)
    if not devices:
        await edit_nav(cb, services, "Устройств без профиля нет.", await _main_menu_markup(services))
    else:
        await edit(cb, "📦 Устройства без профиля:", kb.unassigned_devices(devices))
    await cb.answer()


async def _device_back_target_and_label(services, dev) -> tuple[str, str]:
    """Вычисляет (back_target, reassign_label) ИЗ ПРИНАДЛЕЖНОСТИ устройства —
    не тащим контекст «откуда пришли» через цепочку колбэков, поэтому карточка
    корректна независимо от точки входа (свои устройства / устройства клиента /
    устройства без клиента)."""
    service_id = await call(services.db.get_service_client_id)
    if dev.client_id == service_id:
        return Menu(action="unassigned").pack(), "🔀 Передать в другой профиль"
    admin_own = await call(services.admin_client)
    if admin_own and dev.client_id == admin_own.id:
        return Menu(action="main").pack(), "🔀 Передать в другой профиль"
    return (ClientCB(action="devices", client_id=dev.client_id).pack(),
            "🔀 Передать в другой профиль")


@router.callback_query(DeviceCB.filter(F.action == "open"))
async def admin_device_open(cb: CallbackQuery, callback_data: DeviceCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, *await _device_card_parts(services, dev))
    await cb.answer()


async def _device_card_parts(services, dev):
    """(текст, клавиатура) карточки устройства для админа; «Назад» и подпись
    привязки — из принадлежности устройства. Шлюз — своя карточка."""
    back_target, reassign_label = await _device_back_target_and_label(services, dev)
    if dev.is_gateway:
        return (texts.gateway_device_card(dev),
                kb.gateway_device_actions(dev, back_target=back_target))
    text = texts.device_card_text(dev, for_admin=True)
    marker = texts.friend_marker(dev)
    if marker:
        text += f"\n\n{marker}"
    return text, kb.device_actions(dev, is_admin=True, back_target=back_target,
                                   reassign_label=reassign_label)


@router.callback_query(DeviceCB.filter(F.action == "connect_menu"))
async def admin_device_connect_menu(cb: CallbackQuery, callback_data: DeviceCB, services):
    """«Как планируешь подключить устройство?» — назад к карточке этого же
    устройства."""
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if not dev.private_key:
        # пир, подхваченный с сервера: ссылки нет и взять её неоткуда
        await edit(cb, texts.UNMANAGED_DEVICE_DIALOG, kb.unmanaged_device_dialog(dev.id))
        await cb.answer()
        return
    back = DeviceCB(action="open", device_id=dev.id).pack()
    await edit(cb, texts.CONNECT_METHOD_ASK, kb.connect_method_choice(dev.id, back))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "reassign"))
async def device_reassign_start(cb: CallbackQuery, callback_data: DeviceCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    clients = await call(services.db.list_clients)              # без служебного, с админом
    # исключаем текущего клиента устройства — перепривязывать на него же незачем
    clients = [c for c in clients if c.id != dev.client_id]
    if not clients:
        await cb.answer("Нет других профилей для привязки", show_alert=True)
        return
    await edit(cb, "К какому профилю привязать устройство?",
               kb.reassign_targets(callback_data.device_id, clients))
    await cb.answer()


@router.callback_query(ReassignCB.filter(F.stage == "go"))
async def device_reassign_apply(cb: CallbackQuery, callback_data: ReassignCB, services):
    """Привязка: если у клиента есть слот — сразу; иначе спрашиваем про слот."""
    has_slot = await call(services.has_free_slot, callback_data.client_id)
    if not has_slot:
        client = await call(services.db.get_client, callback_data.client_id)
        limit = client.device_limit if client else "?"
        kbd = kb.reassign_addslot(callback_data.device_id, callback_data.client_id)
        await edit(cb, f"У профиля лимит устройств исчерпан ({limit} из {limit}).\n"
                       f"Добавить слот под это устройство?", kbd)
        await cb.answer()
        return
    await _do_reassign(cb, services, callback_data.device_id, callback_data.client_id, add_slot=False)


@router.callback_query(ReassignCB.filter(F.stage == "slot_yes"))
async def device_reassign_slot_yes(cb: CallbackQuery, callback_data: ReassignCB, services):
    await _do_reassign(cb, services, callback_data.device_id, callback_data.client_id, add_slot=True)


@router.callback_query(ReassignCB.filter(F.stage == "slot_no"))
async def device_reassign_slot_no(cb: CallbackQuery, callback_data: ReassignCB, services):
    await edit_nav(cb, services, "Отменено — устройство не привязано.", await _main_menu_markup(services))
    await cb.answer()


async def _do_reassign(cb, services, device_id, client_id, *, add_slot: bool):
    try:
        info = await call(services.reassign_device, device_id, client_id, add_slot)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await edit_nav(cb, services, "✅ Устройство привязано к профилю.", await _main_menu_markup(services))
    # уведомляем ПОЛУЧАТЕЛЯ (с обогащением, если добавлен слот)
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
    # уведомляем ДОНОРА (если он реальный клиент с tg — не служебный)
    donor = info["donor"]
    if donor and donor["tg_id"]:
        await notify_one(cb.bot, donor["tg_id"],
                         texts.reassign_donor_notice(info["name"], donor["count"], donor["limit"]))
    # держатель переданного устройства теряет его: переезд к другому владельцу
    if info.get("holder_tg"):
        await notify_one(cb.bot, info["holder_tg"], texts.lent_device_reassigned_notice(info["name"]))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "edit_name"))
async def device_edit_name_start(cb: CallbackQuery, callback_data: DeviceCB, services, state: FSMContext):
    await state.set_state(EditDeviceName.value)
    await state.update_data(device_id=callback_data.device_id)
    await ask_tracked(cb.message, services, "Введи новое имя устройства:", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(EditDeviceName.value)
async def device_edit_name_apply(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, "Имя не может быть пустым:")
        return
    data = await state.get_data()
    await state.clear()
    old_dev = await call(services.db.get_device, data["device_id"])
    old_name = old_dev.name if old_dev else "?"
    try:
        await call(services.rename_device, data["device_id"], name)
    except ServiceError as e:
        await message.answer(str(e), reply_markup=kb.reply_hide())
        await _return_panel(message, services)
        return
    await message.answer(f"✅ Устройство переименовано: «{old_name}» → «{name}».",
                         reply_markup=kb.reply_hide())
    await _return_panel(message, services)


@router.callback_query(Menu.filter(F.action == "add_device_choice"))
async def admin_add_device_choice(cb: CallbackQuery, services):
    """«Добавить устройство» из главного меню — сначала спрашиваем, кому:
    себе или конкретному клиенту."""
    await edit(cb, "Кому добавить устройство?", kb.admin_add_device_choice())
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "add_device_pick"))
async def admin_add_device_pick(cb: CallbackQuery, services):
    """Список клиентов для «Добавить устройство → другому клиенту». Дальше —
    тот же FSM-флоу, что и из карточки клиента (ClientCB add_device уже
    обрабатывается admin_add_device_start)."""
    clients = await call(services.db.list_clients, exclude_tg=config.ADMIN_ID)
    if not clients:
        await cb.answer("Профилей пока нет", show_alert=True)
        return
    await edit(cb, "Кому из профилей добавить устройство?",
               kb.pick_client_for_add_device(clients))
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
    await cb.answer()


@router.callback_query(DelDeviceCB.filter(F.stage == "ask"))
async def admin_del_ask(cb: CallbackQuery, callback_data: DelDeviceCB, services):
    """Усиленный поток удаления (из списков устройств) — админская версия.
    Ownership не проверяем: админ управляет любыми устройствами."""
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    only = await call(services.is_only_device, dev.id)
    if only:
        await edit(cb, texts.DELETE_ONLY_DEVICE_WARNING,
                   kb.confirm_delete_device(dev.id, only=True))
    else:
        await edit(cb, texts.DELETE_DEVICE_CONFIRM.format(name=texts._e(dev.name)),
                   kb.confirm_delete_device(dev.id, only=False))
    await cb.answer()


@router.callback_query(DelDeviceCB.filter(F.stage == "confirm"))
async def admin_del_confirm(cb: CallbackQuery, callback_data: DelDeviceCB, services):
    _dev = await call(services.db.get_device, callback_data.device_id)
    _dname = _dev.name if _dev else "?"
    _cl = await call(services.db.get_client, _dev.client_id) if _dev else None
    _cname = _cl.name if _cl else None
    try:
        await remove_device_and_notify(cb.bot, services, callback_data.device_id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    txt = (f"🗑 Устройство «{_dname}» (профиль «{_cname}») удалено."
           if _cname else f"🗑 Устройство «{_dname}» удалено.")
    await edit_nav(cb, services, txt, await _main_menu_markup(services))
    await cb.answer()
