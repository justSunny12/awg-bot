"""
handlers/client.py — роутер клиента (и активация инвайта).

Тонкие обработчики: приняли → проверили владение → позвали services → отрисовали.
Тяжёлые вызовы идут через common.call (to_thread). Статус сервера в приветствии —
из кэша монитора (0 docker exec на /start).

Ввод текста — на месте экрана (ask_here) с инлайн «✖️ Отмена»; после ввода
бот рисует экран-контекст заново с итогом первой строкой (back_to_context).
Числа — пресетами (PresetCB), «✏️ Другое» — ввод.
"""

from __future__ import annotations

import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import (BlockCB, CancelCB, DelDeviceCB, DeviceCB, GraceCB, HelpCB, Menu,
                                  PauseCB, PresetCB)
from awgbot.bot.filters import RoleFilter
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.bot.handlers.common import (
    call, cleanup_content, drop_message, edit, edit_nav, ask_here, ask_tracked, back_to_context,
    own_device, mine_or_held, purge_menus, remove_device_and_notify, send_device_config,
    send_menu, content_finisher, show_screen)
from awgbot.domain.services import BYTES_PER_GB, LimitReached, ServiceError
from awgbot.bot.states import AddDevice, EditDeviceName, EditTrafficLimit, PauseDays
from awgbot.core.enums import PauseMode, PeriodKind, SubStatus

router = Router(name="client")
router.message.filter(RoleFilter("client", "activation"))
router.callback_query.filter(RoleFilter("client"))

ADD_FROM_DEVICES = kb.ADD_FROM_DEVICES   # «➕ Устройство» из списка устройств


def _bot_username(services) -> str:
    return getattr(services, "bot_username", "") or ""


# ── главная ──────────────────────────────────────────────────────────────────

async def main_payload(services, client):
    """(текст, клавиатура) главной клиента."""
    server_ok = await call(services.server_ok_cached)     # 0 exec: статус из state
    slots = await call(services.device_slots, client.id)
    routing_ok = await call(services.routing_health_for_client, client)
    held = await call(services.db.list_held_devices, client.id)
    traffic = await call(services.db.get_client_traffic, client.id)
    routing_visible = await call(services.routing_client_visible, client)
    routing_on = await call(services.routing_profile_on, client.id) if routing_visible else False
    text = texts.greeting_client(client, server_ok, slots, routing_ok, held=held, traffic=traffic,
                                 routing_on=routing_on, bot_username=_bot_username(services))
    used, _limit = slots
    markup = kb.client_main(has_devices=used > 0 or bool(held), routing_visible=routing_visible,
                            client_id=client.id)
    return text, markup


async def _show_main(target, services, client, *, via_edit=None):
    """Главная одним заходом. Держит инвариант «одно активное меню»: новое
    гасит прежнее. Стирает промежуточные служебные сообщения диалога."""
    text, markup = await main_payload(services, client)
    if via_edit is not None:
        await cleanup_content(via_edit.bot, services, via_edit.message.chat.id)
        await edit_nav(via_edit, services, text, markup)
    else:
        await cleanup_content(target.bot, services, target.chat.id)
        await send_menu(target, services, text, markup)


# ── /start и активация ───────────────────────────────────────────────────────

@router.message(CommandStart(deep_link=True), RoleFilter("activation"))
async def start_activation(message: Message, command: CommandObject, services, state: FSMContext):
    """Переход по ссылке-инвайту /start {код}."""
    await state.clear()
    code = (command.args or "").strip()
    await _try_activate(message, services, code)


@router.message(CommandStart(), RoleFilter("activation"))
async def start_cold(message: Message, state: FSMContext):
    """Холодный старт: незнакомец нажал «Запустить» без кода-инвайта."""
    await state.clear()
    await message.answer(texts.COLD_START_GREETING)


@router.message(Command("code"), RoleFilter("activation"))
async def code_activation(message: Message, command: CommandObject, services, state: FSMContext):
    await state.clear()
    code = (command.args or "").strip()
    if not code:
        await message.answer(texts.CODE_NO_ARG)
        return
    await _try_activate(message, services, code)


async def _try_activate(message: Message, services, code: str):
    code = code.strip()
    if code[:1] == "F":
        await _activate_friend(message, services, code)
        return
    res = await call(services.activate_client, code, message.from_user.id)
    if not res.ok:
        if res.reason == "already_has_access":
            await message.answer(texts.ACTIVATION_ALREADY)
        else:
            await message.answer(texts.ACTIVATION_INVALID)
        return
    # одно сообщение: «доступ открыт» и сразу выбор устройства для гайда
    await send_menu(message, services, texts.ACTIVATION_OK_HELP, kb.help_menu(is_initial=True))
    u = message.from_user
    handle = f"@{u.username}" if u.username else (u.full_name or str(u.id))
    if settings.get_bool("notifications.client_events.activation", True):
        await notify_one(message.bot, config.ADMIN_ID,
                         texts.activated_admin_notice(res.client.name, handle))


async def _activate_friend(message: Message, services, code: str):
    """Активация кода друга (F…) незнакомцем: заводится гостевой профиль."""
    u = message.from_user
    res = await call(services.activate_friend, code, u.id, (u.first_name or u.username or ""))
    if not res.ok:
        if res.reason == "already_user":
            await message.answer(texts.FRIEND_ALREADY_USER)
        else:
            await message.answer(texts.ACTIVATION_INVALID)
        return
    await message.answer(texts.friend_activated(res.device_name))
    from awgbot.bot.handlers.friend import show_guest_main
    await show_guest_main(message, services, res.holder)
    await _notify_owner_activated(message, services, res)


async def _notify_owner_activated(message: Message, services, res) -> None:
    dev = await call(services.db.get_device, res.device_id)
    host = await call(services.db.get_client, dev.client_id) if dev else None
    u = message.from_user
    handle = f"@{u.username}" if u.username else (u.full_name or str(u.id))
    if host and host.tg_id:
        await notify_one(message.bot, host.tg_id,
                         texts.friend_activated_host_notice(dev.name, handle))


async def take_code_as_member(message: Message, services, client, code: str) -> None:
    """Код от того, у кого уже есть профиль — гость или клиент.

    F… — ещё одно устройство от того же владельца (иной владелец — отказ, код
    цел). C… — у клиента отказ «уже есть доступ»; у гостя — переход во
    владельцы с переносом всех переданных устройств.
    """
    code = code.strip()
    u = message.from_user
    if code[:1] == "F":
        res = await call(services.activate_friend, code, u.id, (u.first_name or u.username or ""))
        if not res.ok:
            if res.reason == "other_donor":
                await message.answer(texts.friend_other_donor_refusal(res.held, res.donor))
            elif res.reason == "own_device":
                await message.answer("Это твоё собственное устройство — держать его незачем 🙂")
            else:
                await message.answer(texts.ACTIVATION_INVALID)
            return
        holder = res.holder
        own_slots = None if holder.is_guest else await call(services.device_slots, holder.id)
        dev = await call(services.db.get_device, res.device_id)
        await message.answer(texts.friend_device_added(dev, res.donor, len(res.held), own_slots))
        if holder.is_guest:
            from awgbot.bot.handlers.friend import show_guest_main
            await show_guest_main(message, services, holder)
        else:
            await _show_main(message, services, holder)
        await _notify_owner_activated(message, services, res)
        return
    if not client.is_guest:
        await message.answer(texts.ACTIVATION_ALREADY)
        return
    res = await call(services.activate_client, code, u.id)
    if not res.ok:
        await message.answer(texts.ACTIVATION_INVALID)
        return
    up, new = res.upgrade, res.client
    handle = f"@{u.username}" if u.username else (u.full_name or str(u.id))
    if up is not None and up.moved:
        await message.answer(texts.guest_upgraded(up.donor, up.moved, new.device_limit))
        if up.donor is not None and up.donor.tg_id:
            used, limit = await call(services.device_slots, up.donor.id)
            await notify_one(message.bot, up.donor.tg_id,
                             texts.guest_upgraded_donor_notice(up.moved, new, used, limit))
        admin_text = (texts.activated_admin_notice(new.name, handle)
                      + texts.guest_upgraded_admin_tail(up.donor, up.moved, new.device_limit))
    else:
        await message.answer(texts.ACTIVATION_OK)
        admin_text = texts.activated_admin_notice(new.name, handle)
    await _show_main(message, services, new)
    if settings.get_bool("notifications.client_events.activation", True):
        await notify_one(message.bot, config.ADMIN_ID, admin_text)


def parse_link(payload: str) -> tuple[str, int] | None:
    """«sub», «rf», «dev-<id>» → (вид экрана, ref); иначе None (код)."""
    if payload == texts.SUB_PAYLOAD:
        return "sub", 0
    if payload == texts.RF_PAYLOAD_CLIENT:
        return "rf", 0
    head = texts.DEV_PAYLOAD + "-"
    if payload.startswith(head) and payload[len(head):].isdigit():
        return "dev", int(payload[len(head):])
    return None


@router.message(CommandStart(deep_link=True), RoleFilter("client"))
async def start_client_with_code(message: Message, command: CommandObject, client, services,
                                 state: FSMContext):
    """/start {payload} у действующего клиента: ссылка на экран (подписка,
    РФ-доступ, устройство) или чужое устройство ему в держание."""
    await state.clear()
    payload = (command.args or "").strip()
    link = parse_link(payload)
    if link is not None:
        if not await show_screen(message, services, "client", client, *link):
            await _show_main(message, services, client)
        return
    await take_code_as_member(message, services, client, payload)


@router.message(Command("code"), RoleFilter("client"))
async def code_client(message: Message, command: CommandObject, client, services, state: FSMContext):
    await state.clear()
    code = (command.args or "").strip()
    if not code:
        await message.answer(texts.CODE_NO_ARG)
        return
    await take_code_as_member(message, services, client, code)


@router.message(CommandStart(), RoleFilter("client"))
async def start_client(message: Message, client, services, state: FSMContext):
    await state.clear()
    await purge_menus(message.bot, services, message.chat.id)   # /start = заново
    await _show_main(message, services, client)


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


_info_parts = sub_parts


@router.callback_query(Menu.filter(F.action == "info"))
async def menu_info(cb: CallbackQuery, client, services):
    parts = await sub_parts(services, client.id)
    if parts is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    await edit(cb, *parts)
    await cb.answer()


async def devices_payload(services, client, chat_id: int = 0):
    devices = await call(services.db.list_devices, client.id)
    held = await call(services.db.list_held_devices, client.id)
    used, limit = await call(services.device_slots, client.id)
    from awgbot.bot import paging
    return (texts.devices_header(used, limit, held),
            kb.client_devices(devices, held, page=paging.page_of(chat_id or client.tg_id, "devices")))


_devices_payload = devices_payload


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
        await cb.answer("Сначала добавь устройство", show_alert=True)
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
    await drop_message(cb)                           # меню не должно висеть над ссылкой
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


_device_card_parts = device_card_parts


@router.callback_query(DeviceCB.filter(F.action == "open"))
async def device_open(cb: CallbackQuery, callback_data: DeviceCB, client, services, state: FSMContext):
    await state.clear()
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, *await device_card_parts(services, client, dev))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "edit_name"))
async def client_device_edit_name_start(cb: CallbackQuery, callback_data: DeviceCB,
                                        client, services, state: FSMContext):
    """Переименование СВОЕГО устройства — приглашение на месте карточки."""
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
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
        await cb.answer("Устройство не найдено", show_alert=True)
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
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await state.clear()
    plimit = await call(services.profile_traffic_limit, dev.client_id)
    await edit(cb, texts.device_limit_prompt(dev.name, plimit),
               kb.device_limit_kb(dev.id, plimit, DeviceCB(action="open", device_id=dev.id)))
    await cb.answer()


async def _apply_device_limit(cb: CallbackQuery, services, client, dev, gb_value: int) -> None:
    plimit = await call(services.profile_traffic_limit, dev.client_id)
    if plimit and gb_value * BYTES_PER_GB > plimit:
        await cb.answer(texts.device_limit_over(plimit), show_alert=True)
        return
    old_b = int(dev.traffic_limit)
    new_b = gb_value * BYTES_PER_GB
    await call(services.set_device_traffic_limit, dev.id, new_b)
    fresh = await call(services.db.get_device, dev.id)
    text, markup = await device_card_parts(services, client, fresh)
    from awgbot.bot import screens
    await edit(cb, screens.with_note(text, texts.limit_note(old_b, new_b, plimit)), markup)
    await cb.answer()


@router.callback_query(PresetCB.filter((F.kind == "devlimit") & (F.ref > 0)))
async def device_limit_preset(cb: CallbackQuery, callback_data: PresetCB, client, services,
                              state: FSMContext):
    """Пресет лимита существующего устройства; «✏️ Другое» — ввод на месте."""
    dev = await call(own_device, services, client, callback_data.ref)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
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
    plimit = await call(services.profile_traffic_limit, dev.client_id)
    gb_value = int(raw)
    if plimit and gb_value * BYTES_PER_GB > plimit:
        await ask_tracked(message, services, texts.device_limit_over(plimit))
        return
    await state.clear()
    old_b = int(dev.traffic_limit)
    new_b = gb_value * BYTES_PER_GB
    await call(services.set_device_traffic_limit, dev.id, new_b)
    await back_to_context(message, services, data, "client", client,
                          note=texts.limit_note(old_b, new_b, plimit))


@router.callback_query(DeviceCB.filter(F.action == "transfer"))
async def device_transfer_ask(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, texts.transfer_ask(dev.name), kb.confirm_transfer(dev.id))
    await cb.answer()


async def _send_invite(cb: CallbackQuery, services, dev, code: str) -> None:
    """Приглашение другу: сообщение с кнопками «📤 Отправить» и «📋
    Скопировать», под ним — пояснение с «⬅️ В меню»."""
    bot = _bot_username(services) or (await cb.bot.me()).username
    await drop_message(cb)
    plain = texts.friend_invite_plain(dev.name, code, bot)
    link = f"https://t.me/{bot}?start={code}"
    sent = await cb.message.answer(texts.friend_invite_message(dev.name, code, bot),
                                   reply_markup=kb.invite_kb(plain, link))
    await call(services.db.add_content_msg_id, sent.chat.id, sent.message_id)
    await content_finisher(cb.message, services, texts.finish_friend_invite(dev.name), "client")


@router.callback_query(DeviceCB.filter(F.action == "transfer_yes"))
async def device_transfer_do(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    try:
        code = await call(services.make_device_friendly, dev.id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await _send_invite(cb, services, dev, code)
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "reinvite"))
async def device_reinvite(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    try:
        code = await call(services.reissue_friend_code, dev.id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await _send_invite(cb, services, dev, code)
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action.in_(kb.GEN_ACTIONS)))
async def device_gen(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    """Выдача по устройству — один обработчик на три вида. Своё или удерживаемое."""
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await _issue(cb, services, client, dev, kb.gen_kind(callback_data.action))


# ── добавление устройства ────────────────────────────────────────────────────

@router.callback_query(DeviceCB.filter(F.action == "add"))
async def device_add_start(cb: CallbackQuery, callback_data: DeviceCB, client, services,
                           state: FSMContext):
    """«➕ Устройство»: лимит исчерпан — список с «🗑», иначе приглашение
    ввода имени на месте экрана с переключателем «для друга»."""
    used, limit = await call(services.device_slots, client.id)
    if limit != 0 and used >= limit:              # 0 = безлимит
        devices = await call(services.db.list_devices, client.id)
        from awgbot.bot import paging
        await edit(cb, "📱 " + texts.limit_exhausted_line(used, limit),
                   kb.pick_device_to_delete(devices, page=paging.page_of(cb.message.chat.id, "deldev")))
        await cb.answer()
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
                        kb.device_limit_kb(0, plimit, CancelCB(kind=data.get("ctx_kind") or "main")))
        return
    await state.clear()
    await _create_own(message, services, client, name)


async def _create_own(message: Message, services, client, name: str) -> None:
    """Своё устройство: без вопроса о лимите (ставится в карточке), сразу
    экран с рядом выдачи."""
    try:
        created = await call(services.add_device, client.id, name, 0)
    except LimitReached:
        await back_to_context(message, services, {}, "client", client, note="⚠️ " + texts.LIMIT_REACHED)
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
        await back_to_context(target, services, {}, "client", client, note="⚠️ " + texts.LIMIT_REACHED)
        return
    except ServiceError as e:
        await back_to_context(target, services, {}, "client", client, note=f"⚠️ {texts._e(str(e))}")
        return
    code = await call(services.make_device_friendly, created.device_id)
    dev = await call(services.db.get_device, created.device_id)
    bot = _bot_username(services) or (await target.bot.me()).username
    await cleanup_content(target.bot, services, target.chat.id)
    plain = texts.friend_invite_plain(dev.name, code, bot)
    link = f"https://t.me/{bot}?start={code}"
    sent = await target.answer(texts.friend_invite_message(dev.name, code, bot),
                               reply_markup=kb.invite_kb(plain, link))
    await call(services.db.add_content_msg_id, sent.chat.id, sent.message_id)
    await content_finisher(target, services, texts.finish_friend_invite(dev.name), "client")


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
    await drop_message(cb)
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
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await _show_delete_prompt(cb, services, client, dev)
    await cb.answer()


@router.callback_query(DelDeviceCB.filter(F.stage == "confirm"))
async def device_delete_confirm(cb: CallbackQuery, callback_data: DelDeviceCB, client, services):
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    by_holder = dev.holder_client_id == client.id
    try:
        if by_holder:
            await call(services.remove_device, dev.id)      # «удалено владельцем» не про него
        elif dev.is_lent:
            await call(services.remove_device, dev.id)
            if dev.holder_tg_id:
                await notify_one(cb.bot, dev.holder_tg_id, texts.lent_device_deleted_by_owner_notice(dev))
        else:
            await remove_device_and_notify(cb.bot, services, dev.id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer()
    if by_holder:
        if dev.owner_tg_id:
            used, limit = await call(services.device_slots, dev.client_id)
            await notify_one(cb.bot, dev.owner_tg_id,
                             texts.lent_device_deleted_by_holder_notice(dev, used, limit))
        await edit(cb, f"🗑 {texts._e(dev.name)} удалено", None)
        await send_menu(cb.message, services, *await devices_payload(services, client),
                        keep_id=cb.message.message_id)
        return
    # итог — на месте вопроса и остаётся в чате; следом — «Устройства», а если
    # удалили последнее — главная
    devices = await call(services.db.list_devices, client.id)
    used, limit = await call(services.device_slots, client.id)
    await edit(cb, texts.device_deleted(dev.name, used, limit), None)
    if not devices and not await call(services.db.list_held_devices, client.id):
        await _show_main(cb.message, services, client)
        return
    await send_menu(cb.message, services, *await devices_payload(services, client),
                    keep_id=cb.message.message_id)


# ── помощь (гайды — в handlers/guide.py) ─────────────────────────────────────

@router.callback_query(HelpCB.filter(F.platform == "root"))
async def help_root(cb: CallbackQuery):
    await edit(cb, texts.HELP_INTRO, kb.help_menu())
    await cb.answer()


@router.callback_query(HelpCB.filter(F.platform == "skip"))
async def help_skip(cb: CallbackQuery, client, services):
    await _show_main(None, services, client, via_edit=cb)
    await cb.answer()


@router.callback_query(GraceCB.filter(F.action == "take"))
async def grace_take(cb: CallbackQuery, callback_data: GraceCB, client, services):
    """Клиент активирует отсрочку. Защита от протухшей кнопки — внутри
    activate_grace (истёк/использовано/не годовой → неактуально)."""
    if callback_data.ref != client.id:
        await cb.answer(texts.GRACE_STALE, show_alert=True)
        return
    grace_days = settings.get_int("grace.grace_days", 14)
    ok, new_end = await call(services.activate_grace, client.id, grace_days)
    if not ok:
        await cb.answer(texts.GRACE_STALE, show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                              # noqa: BLE001
            pass
        return
    await edit(cb, texts.grace_activated_client(grace_days, timeutil.fmt_date_ui(new_end)), None)
    if settings.get_bool("notifications.client_events.grace", True):
        await notify_one(cb.message.bot, config.ADMIN_ID,
                         texts.grace_activated_admin(client.name, grace_days))
    await cb.answer("Продлено")


# ── ручная блокировка своего устройства ──────────────────────────────────────

from awgbot.core.blocks import DeviceBlock as DeviceBlock


async def _blockable(services, client, callback_data: BlockCB):
    """Своё (не переданное — там управляет держатель) или удерживаемое чужое."""
    if callback_data.target != "dev":
        return None
    dev = await call(mine_or_held, services, client, callback_data.ref)
    if dev is None or (dev.is_lent and dev.holder_client_id != client.id):
        return None
    return dev


@router.callback_query(BlockCB.filter(F.action == "menu_block"))
async def client_block_ask(cb: CallbackQuery, callback_data: BlockCB, client, services):
    """Блокировка — с подтверждением: может отрезать от бота."""
    dev = await _blockable(services, client, callback_data)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, texts.block_device_ask(dev.name), kb.block_device_confirm(dev.id))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "block"))
async def client_block_device(cb: CallbackQuery, callback_data: BlockCB, client, services):
    dev = await _blockable(services, client, callback_data)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    notes = await call(services.block_device_manual, dev.id, DeviceBlock.USER, True)
    await send_notifications(cb.bot, notes)
    dev = await call(services.db.get_device, dev.id)
    await edit(cb, *await device_card_parts(services, client, dev))
    await cb.answer("Заблокировано")


@router.callback_query(BlockCB.filter(F.action == "menu_unblock"))
async def client_unblock_device(cb: CallbackQuery, callback_data: BlockCB, client, services):
    """Снимает ТОЛЬКО свой USER-бит; админские биты остаются."""
    dev = await _blockable(services, client, callback_data)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if not (int(dev.block_reason) & int(DeviceBlock.USER)):
        await cb.answer("Ты не блокировал это устройство", show_alert=True)
        return
    notes = await call(services.unblock_device_manual, dev.id, DeviceBlock.USER, True)
    await send_notifications(cb.bot, notes)
    dev = await call(services.db.get_device, dev.id)
    await edit(cb, *await device_card_parts(services, client, dev))
    await cb.answer("Разблокировано")


# ── пауза подписки ───────────────────────────────────────────────────────────

async def _show_sub(cb, client, services):
    parts = await sub_parts(services, client.id)
    if parts is not None:
        await edit(cb, *parts)


_show_info = _show_sub


@router.callback_query(PauseCB.filter(F.action == "ask"))
async def pause_ask(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    await state.clear()
    avail = await call(services.pause_available_days, client.id)
    if avail <= 0:
        if client.period_kind == PeriodKind.YEAR:
            await cb.answer(texts.pause_limit_exhausted(), show_alert=True)
        else:
            await cb.answer(texts.pause_unavailable(), show_alert=True)
        return
    email = await call(services.email_resume_enabled)
    await edit(cb, texts.pause_ask(avail, email_resume=bool(email)), kb.pause_kb(client.id, avail))
    await cb.answer()


async def _enter_pause(cb_or_msg, services, client, days: int, *, via_cb: CallbackQuery | None) -> None:
    """Поставить паузу и показать итог: сообщение-след, аварийный код (если
    почтовый выход включён), экран подписки следом."""
    ok, reserved, notes, code = await call(services.enter_pause, client.id, days or None)
    if not ok:
        if via_cb is not None:
            await via_cb.answer(texts.pause_unavailable(), show_alert=True)
            await _show_sub(via_cb, client, services)
        else:
            await back_to_context(cb_or_msg, services, {"ctx_kind": "sub"}, "client", client,
                                  note="⚠️ " + texts.pause_unavailable())
        return
    await send_notifications(cb_or_msg.bot, notes)     # друзьям — о постановке
    fresh = await call(services.db.get_client, client.id)
    until = timeutil.fmt_dt_ui(
        timeutil.parse_iso(fresh.pause_active_since)
        + datetime.timedelta(days=int(fresh.pause_reserved_days)))
    summary = texts.pause_entered_summary(until)
    message = via_cb.message if via_cb is not None else cb_or_msg
    await cleanup_content(message.bot, services, message.chat.id)
    if via_cb is not None:
        await via_cb.answer(f"Пауза: {reserved} дн.")
        await edit(via_cb, summary, None)
        keep = message.message_id
    else:
        sent = await message.answer(summary)
        keep = sent.message_id
    if code and await call(services.email_resume_enabled):
        await message.answer(texts.pause_emergency_code(code, await call(services.email_resume_address)))
    parts = await sub_parts(services, client.id)
    if parts is not None:
        await send_menu(message, services, *parts, keep_id=keep)


@router.callback_query(PauseCB.filter(F.action == "pick"))
async def pause_pick(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    """Выбор дней = подтверждение: пауза ставится сразу."""
    await state.clear()
    avail = await call(services.pause_available_days, client.id)
    days = max(1, min(int(callback_data.days), avail))
    await _enter_pause(cb, services, client, days, via_cb=cb)


@router.callback_query(PauseCB.filter(F.action == "other"))
async def pause_other(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    avail = await call(services.pause_available_days, client.id)
    await state.set_state(PauseDays.value)
    await ask_here(cb, services, state, texts.pause_other_prompt(avail), "sub", client_id=client.id)
    await cb.answer()


@router.message(PauseDays.value, RoleFilter("client"))
async def pause_other_apply(message: Message, client, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    avail = await call(services.pause_available_days, client.id)
    if not raw.isdigit() or not (1 <= int(raw) <= avail):
        await ask_tracked(message, services, texts.pause_days_bad(avail))
        return
    await state.clear()
    await _enter_pause(message, services, client, int(raw), via_cb=None)


async def _user_pause_guard(cb, client, services) -> bool:
    """True — у клиента активна ЕГО СОБСТВЕННАЯ пауза (mode=user)."""
    fresh = await call(services.db.get_client, client.id)
    if fresh is None or not fresh.is_paused:
        await cb.answer("Подписка не на паузе", show_alert=True)
        await _show_sub(cb, client, services)
        return False
    if fresh.pause_mode != PauseMode.USER:
        await cb.answer("Эту паузу поставил администратор — снять её может только он",
                        show_alert=True)
        await _show_sub(cb, client, services)
        return False
    return True


@router.callback_query(PauseCB.filter(F.action == "resume"))
async def pause_resume(cb: CallbackQuery, callback_data: PauseCB, client, services):
    """Снять паузу — сразу: сколько спишется, видно на экране подписки."""
    if not await _user_pause_guard(cb, client, services):
        return
    ok, actual, new_end, notes = await call(services.exit_pause, client.id, auto=False)
    if not ok:
        await cb.answer("Подписка не на паузе", show_alert=True)
        await _show_sub(cb, client, services)
        return
    await send_notifications(cb.bot, notes)     # друзьям — о снятии
    await cb.answer("Пауза снята")
    await edit(cb, texts.pause_resumed_self(actual, new_end), None)
    parts = await sub_parts(services, client.id)
    if parts is not None:
        await send_menu(cb.message, services, *parts, keep_id=cb.message.message_id)


@router.callback_query(PauseCB.filter(F.action == "cancel"))
async def pause_cancel(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    await state.clear()
    await _show_sub(cb, client, services)
    await cb.answer()


__all__ = ["router", "main_payload", "devices_payload", "device_card_parts", "sub_parts", "parse_link"]
