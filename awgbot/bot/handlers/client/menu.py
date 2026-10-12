"""menu.py — часть пакета client (см. __init__): меню, «💳 Подписка», выдача, карточка устройства и её действия."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.bot.callbacks import DeviceCB, Menu, PresetCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import (
    call, drop_message, edit, ask_here, ask_tracked, back_to_context, own_device, mine_or_held,
    send_device_config, content_finisher)
from awgbot.domain.services import ServiceError
from awgbot.bot.states import EditDeviceName, EditTrafficLimit
from awgbot.core.enums import PauseMode, SubStatus
from awgbot.bot.handlers import devcore
from .main import router, _bot_username, _show_main


# ── меню ─────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "main"))
async def menu_main(cb: CallbackQuery, client, services, state: FSMContext):
    await state.clear()
    await cb.answer()
    await _show_main(None, services, client, via_edit=cb)


def _pause_flags(client) -> tuple[bool, bool]:
    """(paused_user, can_pause). «Снять» — только своя пауза (mode=user):
    административную снимает админ. «Пауза» — есть срок и дни на счету."""
    from awgbot.core import blocks
    paused_any = (client.is_paused
                  or bool(int(client.block_reason) & int(blocks.ClientBlock.PAUSED)))
    paused_user = client.is_paused and client.pause_mode == PauseMode.USER
    can_pause = (not paused_any and bool(client.period_end)
                 and client.status == SubStatus.ACTIVE
                 and int(client.pause_balance_days) > 0)
    return paused_user, can_pause


async def sub_parts(services, client_id: int):
    """(текст, клавиатура) экрана «💳 Подписка» или None."""
    d = await call(services.client_info_data, client_id)
    if d is None:
        return None
    client = d["client"]
    paused_user, can_pause = _pause_flags(client)
    return (texts.subscription_text(client, routing_visible=d["routing"]),
            kb.subscription_kb(client.id, paused_user=paused_user, can_pause=can_pause))


@router.callback_query(Menu.filter(F.action == "info"))
async def menu_info(cb: CallbackQuery, client, services):
    parts = await sub_parts(services, client.id)
    if parts is None:
        await cb.answer(ui.Toast.no_profile, show_alert=True)
        return
    await edit(cb, *parts)
    await cb.answer()


async def devices_payload(services, client, chat_id: int = 0):
    devices = await call(services.db.list_devices, client.id)
    held = await call(services.db.list_held_devices, client.id)
    used, limit = await call(services.device_quota, client.id)
    from awgbot.bot import paging
    return (texts.devices_header(used, limit, held),
            kb.client_devices(devices, held, page=paging.page_of(chat_id or client.tg_id, "devices"),
                              add=not limit or used < limit))


@router.callback_query(Menu.filter(F.action == "devices"))
async def menu_devices(cb: CallbackQuery, client, services):
    await edit(cb, *await devices_payload(services, client, cb.message.chat.id))
    await cb.answer()


async def _issuable(services, client) -> list:
    """Свои и удерживаемые устройства, которым можно выдать ссылку."""
    own = await call(services.db.list_devices, client.id)
    held = await call(services.db.list_held_devices, client.id)
    return kb.issuable(list(own) + list(held))


@router.callback_query(Menu.filter(F.action.in_(kb.GEN_ACTIONS)))
async def menu_gen_pick(cb: CallbackQuery, callback_data: Menu, client, services):
    """Выдача с главной: одно устройство — сразу, несколько — выбор."""
    devices = await _issuable(services, client)
    if not devices:
        await cb.answer(ui.Toast.add_device_first, show_alert=True)
        return
    if len(devices) == 1:
        await _issue(cb, services, client, devices[0], kb.gen_kind(callback_data.action))
        return
    from awgbot.bot import paging
    await edit(cb, kb.PICK_DEVICE_PROMPT[callback_data.action],
               kb.pick_device(devices, callback_data.action, render=cb.data,
                              page=paging.page_of(cb.message.chat.id, "pick")))
    await cb.answer()


async def _issue(cb: CallbackQuery, services, client, dev, kind: str) -> None:
    """Выдать ссылку/QR/файл одним сообщением с пояснением и «⬅️ В меню»."""
    if not dev.private_key:
        await edit(cb, texts.UNMANAGED_DEVICE_DIALOG, kb.unmanaged_device_dialog(dev.id))
        await cb.answer()
        return
    await drop_message(cb, services)                 # меню не должно висеть над ссылкой
    try:
        await send_device_config(cb.message, services, dev, kind, finisher=kb.to_menu())
    except ServiceError as e:
        await cb.message.answer(texts._e(str(e)))
        await _show_main(cb.message, services, client)
    await cb.answer()


# ── устройство ───────────────────────────────────────────────────────────────

async def device_card_parts(services, client, dev):
    """Карточка с точки зрения клиента: своё — полная; своё переданное — имя,
    лимит, удаление; чужое, которое он держит — карточка держателя."""
    back = Menu(action="devices").pack()
    if dev.holder_client_id == client.id:
        owner = await call(services.db.get_client, dev.client_id)
        return (texts.device_card_held(dev, int(owner.traffic_limit) if owner else 0),
                kb.held_device_actions(dev, back))
    if dev.is_lent:
        return texts.device_card_lent(dev, int(client.traffic_limit)), kb.lent_out_device_actions(dev, back)
    return (texts.device_card_own(dev, int(client.traffic_limit)),
            kb.device_actions(dev, is_admin=False, back_target=back))


@router.callback_query(DeviceCB.filter(F.action == "open"))
async def device_open(cb: CallbackQuery, callback_data: DeviceCB, client, services, state: FSMContext):
    await state.clear()
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await edit(cb, *await device_card_parts(services, client, dev))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "edit_name"))
async def client_device_edit_name_start(cb: CallbackQuery, callback_data: DeviceCB,
                                        client, services, state: FSMContext):
    """Переименование СВОЕГО устройства — приглашение на месте карточки."""
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await state.set_state(EditDeviceName.value)
    await ask_here(cb, services, state, texts.device_name_prompt(dev.name), "dev", dev.id,
                   device_id=dev.id)
    await cb.answer()


@router.message(EditDeviceName.value)
async def client_device_edit_name_apply(message: Message, client, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    data = await state.get_data()
    await state.clear()
    dev = await call(own_device, services, client, data.get("device_id"))
    if dev is None:                     # перепроверка владения на применении
        await back_to_context(message, services, {}, "client", client)
        return
    old_name = dev.name
    try:
        await call(services.rename_device, dev.id, name)
    except ServiceError as e:
        await back_to_context(message, services, data, "client", client, note=f"⚠️ {texts._e(str(e))}")
        return
    await back_to_context(message, services, data, "client", client,
                          note=texts.name_note(old_name, name))


@router.callback_query(DeviceCB.filter(F.action == "connect_menu"))
async def device_connect_menu(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    """Кнопка старого образца «Данные для подключения» → карточка с рядом выдачи."""
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await edit(cb, *await device_card_parts(services, client, dev))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "edit_traffic"))
async def client_edit_device_traffic(cb: CallbackQuery, callback_data: DeviceCB,
                                     client, services, state: FSMContext):
    """Лимит СВОЕГО устройства (включая переданные — они его): пресеты не выше
    лимита профиля на месте карточки."""
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await state.clear()
    plimit = await call(services.profile_traffic_limit, dev.client_id)
    await edit(cb, texts.device_limit_prompt(dev.name, plimit),
               kb.device_limit_kb(dev.id, plimit, DeviceCB(action="open", device_id=dev.id)))
    await cb.answer()


async def _apply_device_limit(cb: CallbackQuery, services, client, dev, gb_value: int) -> None:
    note, ok = await devcore.apply_device_limit(services, dev, gb_value)
    if not ok:
        await cb.answer(note, show_alert=True)
        return
    fresh = await call(services.db.get_device, dev.id)
    text, markup = await device_card_parts(services, client, fresh)
    from awgbot.bot import screens
    await edit(cb, screens.with_note(text, note), markup)
    await cb.answer()


@router.callback_query(PresetCB.filter((F.kind == "devlimit") & (F.ref > 0)))
async def device_limit_preset(cb: CallbackQuery, callback_data: PresetCB, client, services,
                              state: FSMContext):
    """Пресет лимита существующего устройства; «✏️ Другое» — ввод на месте."""
    dev = await call(own_device, services, client, callback_data.ref)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    if callback_data.val < 0:
        plimit = await call(services.profile_traffic_limit, dev.client_id)
        await state.set_state(EditTrafficLimit.value)
        await ask_here(cb, services, state, texts.device_limit_other_prompt(plimit), "dev", dev.id,
                       dev_ref=dev.id)
        await cb.answer()
        return
    await _apply_device_limit(cb, services, client, dev, int(callback_data.val))


@router.message(EditTrafficLimit.value, RoleFilter("client"))
async def client_edit_traffic_apply(message: Message, client, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD)
        return
    data = await state.get_data()
    ref = data.get("dev_ref")
    dev = await call(own_device, services, client, ref) if ref else None
    if dev is None:
        await state.clear()
        await back_to_context(message, services, {}, "client", client)
        return
    note, ok = await devcore.apply_device_limit(services, dev, int(raw))
    if not ok:
        await ask_tracked(message, services, note)
        return
    await state.clear()
    await back_to_context(message, services, data, "client", client, note=note)


@router.callback_query(DeviceCB.filter(F.action == "transfer"))
async def device_transfer_ask(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await edit(cb, texts.transfer_ask(dev.name), kb.confirm_transfer(dev.id))
    await cb.answer()


async def _send_invite(message: Message, services, dev, code: str) -> None:
    """Приглашение другу: сообщение с кнопками «📤 Отправить» и «📋
    Скопировать», под ним — пояснение с «⬅️ В меню»."""
    bot = _bot_username(services) or (await message.bot.me()).username
    plain = texts.friend_invite_plain(dev.name, code, bot)
    link = f"https://t.me/{bot}?start={code}"
    sent = await message.answer(texts.friend_invite_message(dev.name, code, bot),
                                reply_markup=kb.invite_kb(plain, link))
    await call(services.db.add_content_msg_id, sent.chat.id, sent.message_id)
    await content_finisher(message, services, texts.finish_friend_invite(dev.name), "client")


@router.callback_query(DeviceCB.filter(F.action == "transfer_yes"))
async def device_transfer_do(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    try:
        code = await call(services.make_device_friendly, dev.id)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    await drop_message(cb, services)
    await _send_invite(cb.message, services, dev, code)
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "reinvite"))
async def device_reinvite(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    try:
        code = await call(services.reissue_friend_code, dev.id)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    await drop_message(cb, services)
    await _send_invite(cb.message, services, dev, code)
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action.in_(kb.GEN_ACTIONS)))
async def device_gen(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    """Выдача по устройству — один обработчик на три вида. Своё или удерживаемое."""
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await _issue(cb, services, client, dev, kb.gen_kind(callback_data.action))
