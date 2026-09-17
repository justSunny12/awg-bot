"""
handlers/client.py — роутер клиента (и активация инвайта).

Тонкие обработчики: приняли → проверили владение → позвали services → отрисовали.
Тяжёлые вызовы идут через common.call (to_thread). Статус сервера в приветствии —
из кэша монитора (0 docker exec на /start).
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
from awgbot.bot.callbacks import BlockCB, DelDeviceCB, DeviceCB, GraceCB, HelpCB, Menu, PauseCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.bot.handlers.common import (call, cleanup_content, drop_message, edit, edit_nav, ask_tracked, own_device, mine_or_held, purge_menus, park_screen,
                             remove_device_and_notify, send_device_config, send_menu,
                             content_finisher)
from awgbot.domain.services import BYTES_PER_GB, LimitReached, ServiceError
from awgbot.bot.states import AddDevice, EditDeviceName, EditTrafficLimit, PauseDays
from awgbot.core.enums import PauseMode, PeriodKind

router = Router(name="client")
router.message.filter(RoleFilter("client", "activation"))
router.callback_query.filter(RoleFilter("client"))


async def _greeting(services, client):
    server_ok = await call(services.server_ok_cached)     # 0 exec: статус из state
    slots = await call(services.device_slots, client.id)
    routing_ok = await call(services.routing_health_for_client, client)
    held = await call(services.db.list_held_devices, client.id)
    traffic = await call(services.db.get_client_traffic, client.id)
    return texts.greeting_client(client, server_ok, slots, routing_ok, held=held,
                                 traffic=traffic), slots


async def _show_main(target, services, client, *, via_edit=None):
    """Приветствие + меню одним заходом (device_slots считается один раз).
    Держит инвариант «одно активное меню»: новое гасит прежнее. Стирает
    промежуточные служебные сообщения диалога при возврате."""
    text, (used, _) = await _greeting(services, client)
    routing_visible = await call(services.routing_client_visible, client)
    markup = kb.client_main(has_devices=used > 0, routing_visible=routing_visible,
                            client_id=client.id,
                            routing_on=await call(services.routing_profile_on, client.id),
                            manage_sub=_manage_sub(client))
    if via_edit is not None:
        await cleanup_content(via_edit.bot, services, via_edit.message.chat.id)
        await edit_nav(via_edit, services, text, markup)
    else:
        await cleanup_content(target.bot, services, target.chat.id)
        await send_menu(target, services, text, markup)


# ── /start и активация ───────────────────────────────────────────────────────

@router.message(CommandStart(deep_link=True), RoleFilter("activation"))
async def start_activation(message: Message, command: CommandObject, services, state: FSMContext):
    """Переход по ссылке-инвайту /start {код} — логика прежняя."""
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
    """Активация командой /code {код} (холодный вход без deep-link ссылки)."""
    await state.clear()
    code = (command.args or "").strip()
    if not code:
        await message.answer(texts.CODE_NO_ARG)
        return
    await _try_activate(message, services, code)


async def _try_activate(message: Message, services, code: str):
    code = code.strip()
    # Маршрутизация по префиксу кода: F… → друг, всё прочее (C… или старое) → клиент.
    if code[:1] == "F":
        await _activate_friend(message, services, code)
        return
    res = await call(services.activate_client, code, message.from_user.id)
    if not res.ok:
        if res.reason == "already_has_access":
            await message.answer(texts.ACTIVATION_ALREADY)
        else:
            await message.answer(texts.ACTIVATION_INVALID)   # «не помню такого кода…»
        return
    await message.answer(texts.ACTIVATION_OK)
    await send_menu(message, services, texts.HELP_INTRO, kb.help_menu(is_initial=True))
    # уведомить админа об активации (единственная точка для deep-link и /code)
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
    # показать главный экран гостя сразу
    from awgbot.bot.handlers.friend import show_guest_main
    await show_guest_main(message, services, res.holder)
    await _notify_owner_activated(message, services, res)


async def _notify_owner_activated(message: Message, services, res) -> None:
    """Владельцу: друг активировал устройство."""
    dev = await call(services.db.get_device, res.device_id)
    host = await call(services.db.get_client, dev.client_id) if dev else None
    u = message.from_user
    handle = f"@{u.username}" if u.username else (u.full_name or str(u.id))
    if host and host.tg_id:
        await notify_one(message.bot, host.tg_id,
                         texts.friend_activated_host_notice(dev.name, handle))


async def take_code_as_member(message: Message, services, client, code: str) -> None:
    """Код от того, у кого уже есть профиль — гость или клиент (docs/guest-role.md).

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
    await _show_main(message, services, new)           # помощь не предлагаем: уже подключён
    if settings.get_bool("notifications.client_events.activation", True):
        await notify_one(message.bot, config.ADMIN_ID, admin_text)


@router.message(CommandStart(deep_link=True), RoleFilter("client"))
async def start_client_with_code(message: Message, command: CommandObject, client, services,
                                 state: FSMContext):
    """/start {код} у действующего клиента: чужое устройство ему в держание."""
    await state.clear()
    await take_code_as_member(message, services, client, (command.args or "").strip())


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
async def menu_main(cb: CallbackQuery, client, services):
    await cb.answer()
    await _show_main(None, services, client, via_edit=cb)   # cleanup_content — внутри


def _pause_flags(client) -> tuple[bool, bool]:
    """(paused_user, can_pause) для кнопок «Управлять подпиской».
    «Возобновить» — ТОЛЬКО для собственной паузы клиента (mode=user):
    админскую приостановку (admin_fixed/admin_open) клиент снимать не должен —
    её снимает админ вместе с блокировкой. «Приостановить» — годовая подписка
    и никакой активной паузы/PAUSED-бита."""
    from awgbot.core import blocks
    paused_any = (client.is_paused
                  or bool(int(client.block_reason) & int(blocks.ClientBlock.PAUSED)))
    paused_user = client.is_paused and client.pause_mode == PauseMode.USER
    can_pause = (not paused_any and bool(client.period_end)
                 and int(client.pause_balance_days) > 0)
    return paused_user, can_pause


def _manage_sub(client) -> bool:
    """«Управлять» — всем, кроме бессрочных: пауза — их рычаг, и видеть его
    полезно и тем, у кого дней пока нет (за что дают — на экране)."""
    return bool(client.effective_period_end)


@router.callback_query(Menu.filter(F.action == "info"))
async def menu_info(cb: CallbackQuery, client, services):
    parts = await _info_parts(services, client.id)
    if parts is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    await edit(cb, *parts)
    await cb.answer()


async def _devices_payload(services, client):
    devices = await call(services.db.list_devices, client.id)
    held = await call(services.db.list_held_devices, client.id)
    slots = await call(services.device_slots, client.id)
    header = "<b>📱Твои устройства</b>\n\n" + texts.device_slots_line(*slots) + texts.held_devices_tail(held)
    return header, kb.client_devices(devices, held)


@router.callback_query(Menu.filter(F.action == "devices"))
async def menu_devices(cb: CallbackQuery, client, services):
    await edit(cb, *await _devices_payload(services, client))
    await cb.answer()


@router.callback_query(Menu.filter(F.action.in_(kb.GEN_ACTIONS)))
async def menu_gen_pick(cb: CallbackQuery, callback_data: Menu, client, services):
    """Выбор устройства под ссылку/QR/файл — одним обработчиком на три кнопки."""
    devices = kb.issuable(await call(services.db.list_devices, client.id))
    if not devices:
        await cb.answer("Сначала добавь устройство", show_alert=True)
        return
    await edit(cb, kb.PICK_DEVICE_PROMPT[callback_data.action],
               kb.pick_device(devices, callback_data.action))
    await cb.answer()


# ── устройство ───────────────────────────────────────────────────────────────

async def _device_card_parts(services, client, dev):
    """Карточка с точки зрения клиента (docs/guest-role.md): своё — полная;
    своё, но переданное — имя и удаление; чужое, которое он держит — карточка
    держателя."""
    back = Menu(action="devices").pack()
    if dev.holder_client_id == client.id:
        owner = await call(services.db.get_client, dev.client_id)
        return (texts.held_device_card(dev, int(owner.traffic_limit) if owner else 0),
                kb.held_device_actions(dev, back))
    text = texts.device_card_text(dev, for_admin=False)
    if not dev.private_key:
        text += texts.UNMANAGED_DEVICE_EXPLAIN
    if dev.is_lent:
        return text + f"\n\n{texts.lent_out_marker(dev)}", kb.lent_out_device_actions(dev, back)
    marker = texts.friend_marker(dev)
    if marker:
        text += f"\n\n{marker}"
    return text, kb.device_actions(dev, is_admin=False, back_target=back)


@router.callback_query(DeviceCB.filter(F.action == "open"))
async def device_open(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, *await _device_card_parts(services, client, dev))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "edit_name"))
async def client_device_edit_name_start(cb: CallbackQuery, callback_data: DeviceCB,
                                        client, services, state: FSMContext):
    """Клиент переименовывает СВОЁ устройство (own_device — защита от чужого id).
    Устройства друга он переименовывать не может: он ими не владеет."""
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await state.set_state(EditDeviceName.value)
    await state.update_data(device_id=dev.id)
    await ask_tracked(cb.message, services, "Введи новое имя устройства:", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(EditDeviceName.value)
async def client_device_edit_name_apply(message: Message, client, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, "Имя не может быть пустым:")
        return
    data = await state.get_data()
    await state.clear()
    dev = await call(own_device, services, client, data["device_id"])
    if dev is None:                     # перепроверка владения на применении
        await message.answer("Устройство не найдено.", reply_markup=kb.reply_hide())
        await _show_main(message, services, client)
        return
    old_name = dev.name
    try:
        await call(services.rename_device, dev.id, name)
    except ServiceError as e:
        await message.answer(str(e), reply_markup=kb.reply_hide())
        await _show_main(message, services, client)
        return
    # как у админа: итог и меню следом — раньше диалог кончался отчётом без
    # единой кнопки
    await message.answer(f"✅ Устройство переименовано: «{old_name}» → «{name}».",
                         reply_markup=kb.reply_hide())
    await _show_main(message, services, client)


@router.callback_query(DeviceCB.filter(F.action == "connect_menu"))
async def device_connect_menu(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    """«Как планируешь подключить устройство?» — назад к карточке этого же
    устройства. Своё или удерживаемое."""
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if not dev.private_key:
        # ключа нет: ссылку выдать не можем — дружелюбный диалог
        await edit(cb, texts.UNMANAGED_DEVICE_DIALOG, kb.unmanaged_device_dialog(dev.id))
        await cb.answer()
        return
    back = DeviceCB(action="open", device_id=dev.id).pack()
    await edit(cb, texts.CONNECT_METHOD_ASK, kb.connect_method_choice(dev.id, back))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "edit_traffic"))
async def client_edit_device_traffic(cb: CallbackQuery, callback_data: DeviceCB,
                                     client, services, state: FSMContext):
    """Клиент меняет лимит потребления СВОЕГО устройства (включая friend-устройства
    — они принадлежат клиенту). own_device валидирует принадлежность."""
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await state.set_state(EditTrafficLimit.value)
    await state.update_data(kind="device", ref=dev.id)
    plimit = await call(services.profile_traffic_limit, dev.client_id)
    await ask_tracked(cb.message, services, texts.traffic_limit_device_ask(plimit), reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(EditTrafficLimit.value, RoleFilter("client"))
async def client_edit_traffic_apply(message: Message, client, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.TRAFFIC_LIMIT_BAD)
        return
    data = await state.get_data()
    ref = data.get("ref")
    await state.clear()
    # страхуемся: клиент правит только СВОИ устройства
    dev = await call(own_device, services, client, ref) if ref else None
    if dev is None:
        await _show_main(message, services, client)
        return
    old_b = int(dev.traffic_limit)
    new_b = int(raw) * BYTES_PER_GB
    await call(services.set_device_traffic_limit, ref, new_b)
    old_s = "без ограничения" if not old_b else texts.gb_str(old_b)
    new_s = "без ограничения" if not new_b else texts.gb_str(new_b)
    await message.answer(
        f"✅ Устройство «{dev.name}»: лимит потребления {old_s} → {new_s}.",
        reply_markup=kb.reply_hide())
    await _show_main(message, services, client)


@router.callback_query(DeviceCB.filter(F.action == "transfer"))
async def device_transfer_ask(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, texts.TRANSFER_FRIEND_WARNING.format(name=texts._e(dev.name)),
               kb.confirm_transfer(dev.id))
    await cb.answer()


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
    me = await cb.bot.me()
    await drop_message(cb)
    sent = await cb.message.answer(texts.friend_invite_message(dev.name, code, me.username))
    await call(services.db.add_content_msg_id, sent.chat.id, sent.message_id)
    await content_finisher(cb.message, services, texts.FINISH_FRIEND_INVITE, "client")
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
    me = await cb.bot.me()
    await drop_message(cb)
    sent = await cb.message.answer(texts.friend_invite_message(dev.name, code, me.username))
    await call(services.db.add_content_msg_id, sent.chat.id, sent.message_id)
    await content_finisher(cb.message, services, texts.FINISH_FRIEND_INVITE, "client")
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action.in_(kb.GEN_ACTIONS)))
async def device_gen(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    """Выдача по устройству — один обработчик на три вида. Для устройства без
    приватного ключа (пир подхвачен с сервера) вместо ошибки — дружелюбный
    диалог «удали / назад». Своё или удерживаемое."""
    dev = await call(mine_or_held, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if not dev.private_key:
        await edit(cb, texts.UNMANAGED_DEVICE_DIALOG, kb.unmanaged_device_dialog(dev.id))
        await cb.answer()
        return
    kind = kb.gen_kind(callback_data.action)
    await drop_message(cb)                           # убрать старое меню (не висеть над ссылкой)
    try:
        await send_device_config(cb.message, services, dev, kind)
    except ServiceError as e:
        await cb.message.answer(str(e))
        await _show_main(cb.message, services, client)
        await cb.answer()
        return
    await content_finisher(cb.message, services, texts.finish_config(kind, dev.name), "client")
    await cb.answer()


# ── добавление устройства (FSM: имя) ─────────────────────────────────────────

@router.callback_query(DeviceCB.filter(F.action == "add"))
async def device_add_start(cb: CallbackQuery, client, services, state: FSMContext):
    used, limit = await call(services.device_slots, client.id)
    if limit != 0 and used >= limit:              # 0 = безлимит
        devices = await call(services.db.list_devices, client.id)
        await edit(cb, texts.device_slots_line(used, limit), kb.pick_device_to_delete(devices))
        await cb.answer()
        return
    # сначала выбор: себе или другу (до имени)
    await edit(cb, texts.ADD_FOR_WHOM, kb.add_for_whom())
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "add_self"))
async def device_add_self(cb: CallbackQuery, client, services, state: FSMContext):
    used, limit = await call(services.device_slots, client.id)
    await state.set_state(AddDevice.name)
    await state.update_data(for_friend=False)
    await park_screen(cb, services)
    await ask_tracked(cb.message, services,
                      f"{texts.device_slots_line(used, limit)}\n\nВведи имя нового устройства:",
                      reply_markup=kb.reply_cancel())
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "add_friend"))
async def device_add_friend(cb: CallbackQuery, client, services, state: FSMContext):
    used, limit = await call(services.device_slots, client.id)
    await state.set_state(AddDevice.name)
    await state.update_data(for_friend=True)
    await park_screen(cb, services)
    # слоты показываем и здесь: устройство друга занимает слот профиля, и
    # видеть «2 из 3» перед тем, как отдавать его, полезно
    await ask_tracked(cb.message, services,
                      f"{texts.device_slots_line(used, limit)}\n\n"
                      "Создаём устройство для друга. Введи имя устройства "
                      "(его будет видеть друг):", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(AddDevice.name, RoleFilter("client"))
async def device_add_name(message: Message, client, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, "Имя не может быть пустым. Введи ещё раз:")
        return
    await state.update_data(dev_name=name)
    await state.set_state(AddDevice.traffic)
    await ask_tracked(message, services, texts.traffic_limit_device_ask(int(client.traffic_limit)),
                         reply_markup=kb.reply_cancel())


@router.message(AddDevice.traffic, RoleFilter("client"))
async def device_add_traffic(message: Message, client, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.TRAFFIC_LIMIT_BAD)
        return
    data = await state.get_data()
    name = data.get("dev_name")
    for_friend = data.get("for_friend", False)
    tlimit = int(raw) * BYTES_PER_GB
    await state.clear()
    if not name:
        await _show_main(message, services, client)
        return
    try:
        created = await call(services.add_device, client.id, name, tlimit)
    except LimitReached:
        await message.answer(texts.LIMIT_REACHED, reply_markup=kb.reply_hide())
        await _show_main(message, services, client)
        return
    except ServiceError as e:
        await message.answer(f"Не удалось создать устройство: {e}", reply_markup=kb.reply_hide())
        await _show_main(message, services, client)
        return
    if for_friend:
        # помечаем гостевым и отдаём инвайт для пересылки
        code = await call(services.make_device_friendly, created.device_id)
        me = await message.bot.me()
        used, limit = await call(services.device_slots, client.id)
        plimit = await call(services.profile_traffic_limit, client.id)
        await message.answer(
            texts.device_created_report(name, device_count=used, max_devices=limit,
                                        dev_limit_bytes=tlimit, profile_limit_bytes=plimit,
                                        for_friend=True),
            reply_markup=kb.reply_hide())
        sent = await message.answer(
            texts.friend_invite_message(name, code, me.username))
        await call(services.db.add_content_msg_id, sent.chat.id, sent.message_id)
        await content_finisher(message, services, texts.FINISH_FRIEND_INVITE, "client")
        return
    dev_count = len(await call(services.db.list_devices, client.id))
    plimit = await call(services.profile_traffic_limit, client.id)
    await message.answer(
        texts.device_created_report(name, device_count=dev_count,
                                    max_devices=client.device_limit,
                                    dev_limit_bytes=tlimit, profile_limit_bytes=plimit),
        reply_markup=kb.reply_hide())
    dev = await call(services.db.get_device, created.device_id)
    back = Menu(action="main").pack()
    await send_menu(message, services, texts.CONNECT_METHOD_ASK,
                    kb.connect_method_choice(dev.id, back, back_label="⬅️ В меню"))


# ── удаление (усиленное для единственного) ──────────────────────────────────

async def _show_delete_prompt(cb, services, client, dev):
    """Три вопроса: своё (обычный / единственное), своё переданное (у держателя
    пропадёт доступ), удерживаемое чужое (нового не создать — только код)."""
    if dev.holder_client_id == client.id:
        await edit(cb, texts.device_delete_by_holder_ask(dev.name),
                   kb.confirm_delete_device(dev.id, only=False))
        return
    if dev.is_lent:
        await edit(cb, texts.device_delete_by_owner_ask(dev),
                   kb.confirm_delete_device(dev.id, only=False))
        return
    only = await call(services.is_only_device, dev.id)
    if only:
        await edit(cb, texts.DELETE_ONLY_DEVICE_WARNING, kb.confirm_delete_device(dev.id, only=True))
    else:
        await edit(cb, texts.DELETE_DEVICE_CONFIRM.format(name=texts._e(dev.name)),
                   kb.confirm_delete_device(dev.id, only=False))


@router.callback_query(DelDeviceCB.filter(F.stage == "ask"))
async def device_delete_ask(cb: CallbackQuery, callback_data, client, services):
    """Вход в подтверждение удаления (из списка устройств или из карточки —
    оба ведут сюда через DelDeviceCB, кнопка «Удалить» в карточке эмитит
    именно этот колбэк, а не прямое удаление)."""
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
        # владельцу — что удалено и сколько у него теперь
        if dev.owner_tg_id:
            used, limit = await call(services.device_slots, dev.client_id)
            await notify_one(cb.bot, dev.owner_tg_id,
                             texts.lent_device_deleted_by_holder_notice(dev, used, limit))
        await edit(cb, f"🗑 Устройство «{texts._e(dev.name)}» удалено.", None)
        await send_menu(cb.message, services, *await _devices_payload(services, client),
                        keep_id=cb.message.message_id)
        return
    # итог — на месте вопроса и остаётся в чате; следом — «Мои устройства»,
    # а если удалили последнее — сразу главное меню: пустой список с одной
    # кнопкой «Назад» ничего не говорит
    devices = await call(services.db.list_devices, client.id)
    used, limit = await call(services.device_slots, client.id)
    await edit(cb, texts.device_deleted(dev.name, used, limit), None)
    if not devices and not await call(services.db.list_held_devices, client.id):
        await _show_main(cb.message, services, client)
        return
    await send_menu(cb.message, services, *await _devices_payload(services, client),
                    keep_id=cb.message.message_id)


# ── помощь с настройкой (меню; гайды — в handlers/guide.py) ──────────────────

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
    # кнопка принадлежит именно этому клиенту (ref в callback совпадает)
    if callback_data.ref != client.id:
        await cb.answer(texts.GRACE_STALE, show_alert=True)
        return
    grace_days = settings.get_int("grace.grace_days", 14)
    ok, new_end = await call(services.activate_grace, client.id, grace_days)
    if not ok:
        await cb.answer(texts.GRACE_STALE, show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    # итог — на месте предложения: одна правка вместо «снять кнопки» + новое
    # сообщение; вопрос отслужил, а история «предлагали → взял» остаётся в тексте
    await edit(cb, texts.grace_activated_client(grace_days, timeutil.fmt_dt(new_end)), None)
    if settings.get_bool("notifications.client_events.grace", True):
        await notify_one(cb.message.bot, config.ADMIN_ID,
                         texts.grace_activated_admin(client.name, grace_days))
    await cb.answer("Продлено")

# ── Ручная блокировка своего устройства (клиент) ─────────────────────────────
# Клиент ставит/снимает USER-бит на СВОИХ устройствах (в т.ч. friend — они его).
# Всегда «громко»: friend-устройство → друг получает уведомление. Тихого варианта
# у клиента нет. Админские биты клиент не трогает (их снимает только админ).

from awgbot.core.blocks import DeviceBlock as DeviceBlock


async def _blockable(services, client, callback_data: BlockCB):
    """Устройство под блокировку своим битом: своё (не переданное — там
    управляет держатель) или удерживаемое чужое."""
    if callback_data.target != "dev":
        return None
    dev = await call(mine_or_held, services, client, callback_data.ref)
    if dev is None or (dev.is_lent and dev.holder_client_id != client.id):
        return None
    return dev


@router.callback_query(BlockCB.filter(F.action == "menu_block"))
async def client_block_ask(cb: CallbackQuery, callback_data: BlockCB, client, services):
    """Блокировка — с подтверждением: действие с последствиями, а кнопка стоит
    рядом с безобидными."""
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
    await edit(cb, *await _device_card_parts(services, client, dev))
    await cb.answer("Заблокировано")


@router.callback_query(BlockCB.filter(F.action == "menu_unblock"))
async def client_unblock_device(cb: CallbackQuery, callback_data: BlockCB, client, services):
    """Клиент снимает ТОЛЬКО свой USER-бит. Админские биты не трогает — если
    устройство заблокировано и админом, оно останется заблокированным."""
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
    await edit(cb, *await _device_card_parts(services, client, dev))
    await cb.answer("Разблокировано")

# ── Приостановка подписки («в отпуск») ───────────────────────────────────────

async def _info_parts(services, client_id: int):
    """(текст, клавиатура) экрана «Управлять подпиской» или None."""
    d = await call(services.client_info_data, client_id)      # один хоп вместо пяти
    if d is None:
        return None
    client = d["client"]
    paused_user, can_pause = _pause_flags(client)
    return (texts.subscription_manage_text(client, routing_visible=d["routing"]),
            kb.client_info_actions(client, paused=paused_user, can_pause=can_pause))


async def _show_info(cb, client, services):
    """Перерисовать «Управлять подпиской» (после входа/выхода из паузы)."""
    parts = await _info_parts(services, client.id)
    if parts is not None:
        await edit(cb, *parts)


@router.callback_query(PauseCB.filter(F.action == "ask"))
async def pause_ask(cb: CallbackQuery, callback_data: PauseCB, client, services):
    avail = await call(services.pause_available_days, client.id)
    if avail <= 0:
        # кнопка не скрыта (показываем для годовой) — на нажатии объясняем причину:
        # если это годовая с исчерпанным лимитом — конкретный текст, иначе общий.
        if client.period_kind == PeriodKind.YEAR:
            await cb.answer(texts.pause_limit_exhausted(), show_alert=True)
        else:
            await cb.answer(texts.pause_unavailable(), show_alert=True)
        return
    await edit(cb, texts.pause_ask(avail), kb.pause_day_choice(client.id, avail))
    await cb.answer()


@router.callback_query(PauseCB.filter(F.action == "pick"))
async def pause_pick(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    """Выбран пресет дней → предупреждение (deadlock) → подтверждение."""
    await state.clear()
    avail = await call(services.pause_available_days, client.id)
    days = max(1, min(int(callback_data.days), avail))
    await edit(cb, texts.pause_warning(days), kb.pause_confirm(client.id, days))
    await cb.answer()


@router.callback_query(PauseCB.filter(F.action == "other"))
async def pause_other(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    """«Другое» → ввод своего числа дней с клавиатуры."""
    avail = await call(services.pause_available_days, client.id)
    await state.set_state(PauseDays.value)
    await state.update_data(client_id=client.id)
    await park_screen(cb, services)
    await ask_tracked(cb.message, services,
                      f"Введи число дней приостановки (от 1 до {avail}):",
                      reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(PauseDays.value, RoleFilter("client"))
async def pause_other_apply(message: Message, client, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    avail = await call(services.pause_available_days, client.id)
    if not raw.isdigit() or not (1 <= int(raw) <= avail):
        await ask_tracked(message, services,
                          f"Нужно целое число от 1 до {avail}. Попробуй ещё раз:",
                          reply_markup=kb.reply_cancel())
        return
    days = int(raw)
    await state.clear()
    # снять reply-«Отмена» (ввод окончен) — как в остальных диалогах; экран
    # подтверждения — через send_menu: прежний (выбор дней) гаснет
    _acc = await message.answer("Принято.", reply_markup=kb.reply_hide())
    await call(services.db.add_content_msg_id, _acc.chat.id, _acc.message_id)
    await send_menu(message, services, texts.pause_warning(days),
                    kb.pause_confirm(client.id, days))


@router.callback_query(PauseCB.filter(F.action == "confirm"))
async def pause_confirm(cb: CallbackQuery, callback_data: PauseCB, client, services):
    ok, reserved, notes, code = await call(services.enter_pause, client.id, callback_data.days or None)
    if not ok:
        await cb.answer(texts.pause_unavailable(), show_alert=True)
        await _show_info(cb, client, services)
        return
    await send_notifications(cb.bot, notes)     # друзьям — о постановке
    await cb.answer(f"Приостановлено на {reserved} дн.")
    # промежуточные сообщения этого действия («Другое»-ввод, служебное) — стереть
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    # итог остаётся в чате: сообщение подтверждения переписываем в резюме
    fresh = await call(services.db.get_client, client.id)
    until = timeutil.fmt_dt(
        timeutil.parse_iso(fresh.pause_active_since)
        + datetime.timedelta(days=int(fresh.pause_reserved_days)))
    summary = texts.pause_entered_summary(until)
    if code and await call(services.email_resume_enabled):
        # итог — без кнопки (остаётся в чате как запись); кнопка «В меню» — на
        # аварийном сообщении ниже (оно последнее и становится нав-сообщением).
        await edit(cb, summary, None)
        sent = await cb.message.answer(
            texts.pause_emergency_code(code, await call(services.email_resume_address)),
            reply_markup=kb.to_menu())
        await call(services.db.set_nav_message_id, sent.chat.id, sent.message_id)
    else:
        # аварийного сообщения нет — «В меню» на самом итоге
        await edit_nav(cb, services, summary, kb.to_menu())


async def _user_pause_guard(cb, client, services) -> bool:
    """True — у клиента активна ЕГО СОБСТВЕННАЯ пауза (mode=user). Иначе алерт
    (и перерисовка инфобокса): админскую приостановку клиент не снимает —
    протухшая кнопка «Возобновить» не должна давать такую лазейку."""
    fresh = await call(services.db.get_client, client.id)
    if fresh is None or not fresh.is_paused:
        await cb.answer("Подписка не на паузе", show_alert=True)
        await _show_info(cb, client, services)
        return False
    if fresh.pause_mode != PauseMode.USER:
        await cb.answer("Эту приостановку установил администратор — "
                        "снять её может только он.", show_alert=True)
        await _show_info(cb, client, services)
        return False
    return True


@router.callback_query(PauseCB.filter(F.action == "resume_ask"))
async def pause_resume_ask(cb: CallbackQuery, callback_data: PauseCB, client, services):
    """Подтверждение перед досрочным выходом — явно называем, сколько дней
    спишется по факту (не весь зарезервированный остаток)."""
    if not await _user_pause_guard(cb, client, services):
        return
    preview = await call(services.preview_exit_pause, client.id)
    if preview is None:
        await cb.answer("Подписка не на паузе", show_alert=True)
        await _show_info(cb, client, services)
        return
    actual, reserved = preview
    await edit(cb, texts.pause_resume_ask(actual, reserved),
               kb.pause_resume_confirm(client.id))
    await cb.answer()


@router.callback_query(PauseCB.filter(F.action == "resume"))
async def pause_resume(cb: CallbackQuery, callback_data: PauseCB, client, services):
    if not await _user_pause_guard(cb, client, services):
        return
    ok, actual, new_end, notes = await call(services.exit_pause, client.id, auto=False)
    if not ok:
        await cb.answer("Подписка не на паузе", show_alert=True)
        await _show_info(cb, client, services)
        return
    await send_notifications(cb.bot, notes)     # друзьям — о снятии
    await cb.answer("Возобновлено")
    # итог — на месте вопроса, остаётся в чате; экран подписки — следом
    await edit(cb, texts.pause_resumed_self(actual, new_end), None)
    parts = await _info_parts(services, client.id)
    if parts is not None:
        await send_menu(cb.message, services, *parts, keep_id=cb.message.message_id)


@router.callback_query(PauseCB.filter(F.action == "cancel"))
async def pause_cancel(cb: CallbackQuery, callback_data: PauseCB, client, services):
    await _show_info(cb, client, services)
    await cb.answer()


__all__ = ["router"]
