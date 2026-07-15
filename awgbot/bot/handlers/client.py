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
from awgbot.bot.handlers.common import (call, cleanup_content, drop_message, edit, edit_nav, ask_tracked, own_device,
                             remove_device_and_notify, send_device_config, send_menu,
                             content_finisher)
from awgbot.domain.services import BYTES_PER_GB, LimitReached, ServiceError
from awgbot.bot.states import AddDevice, EditDeviceName, EditTrafficLimit, PauseDays, RestoreDevice
from awgbot.core.enums import PauseMode, PeriodKind

router = Router(name="client")
router.message.filter(RoleFilter("client", "activation"))
router.callback_query.filter(RoleFilter("client"))


async def _greeting(services, client):
    server_ok = await call(services.server_ok_cached)     # 0 exec: статус из state
    slots = await call(services.device_slots, client.id)
    return texts.greeting_client(client, server_ok, slots), slots


async def _show_main(target, services, client, *, via_edit=None):
    """Приветствие + меню одним заходом (device_slots считается один раз).
    Держит инвариант «одно активное меню»: новое гасит прежнее. Стирает
    промежуточные служебные сообщения диалога при возврате."""
    text, (used, _) = await _greeting(services, client)
    markup = kb.client_main(has_devices=used > 0)
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
    """Активация кода друга (F…): человек получает роль invited над устройством."""
    res = await call(services.activate_friend, code, message.from_user.id)
    if not res.ok:
        if res.reason == "already_user":
            await message.answer(texts.FRIEND_ALREADY_USER)
        else:
            await message.answer(texts.ACTIVATION_INVALID)
        return
    await message.answer(texts.friend_activated(res.device_name))
    # показать гостевую панель сразу
    from awgbot.bot.handlers.friend import show_friend_panel
    await show_friend_panel(message, services, message.from_user.id, fresh=True)
    # уведомить хозяина, что друг активировал устройство
    dev = await call(services.db.get_device, res.device_id)
    host = await call(services.db.get_client, dev.client_id)
    u = message.from_user
    handle = f"@{u.username}" if u.username else (u.full_name or str(u.id))
    if host and host.tg_id:
        await notify_one(message.bot, host.tg_id,
                         texts.friend_activated_host_notice(dev.name, handle))


@router.message(CommandStart(), RoleFilter("client"))
async def start_client(message: Message, client, services, state: FSMContext):
    await state.clear()
    await _show_main(message, services, client)


# ── меню ─────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "main"))
async def menu_main(cb: CallbackQuery, client, services):
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    await _show_main(None, services, client, via_edit=cb)
    await cb.answer()


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
    can_pause = client.period_kind == PeriodKind.YEAR and not paused_any
    return paused_user, can_pause


@router.callback_query(Menu.filter(F.action == "info"))
async def menu_info(cb: CallbackQuery, client, services):
    devices = await call(services.db.list_devices, client.id)
    traffic = await call(services.db.get_client_traffic, client.id)
    online = await call(services.client_is_online, client.id)
    text = texts.subscription_manage_text(client, traffic, online, len(devices))
    paused_user, can_pause = _pause_flags(client)
    await edit(cb, text, kb.client_info_actions(client, paused=paused_user, can_pause=can_pause))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "devices"))
async def menu_devices(cb: CallbackQuery, client, services):
    devices = await call(services.db.list_devices, client.id)
    slots = await call(services.device_slots, client.id)
    header = "<b>📱Твои устройства</b>\n\n" + texts.device_slots_line(*slots)
    await edit(cb, header, kb.client_devices(devices))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "gen_link"))
async def menu_gen_link(cb: CallbackQuery, client, services):
    devices = await call(services.db.list_devices, client.id)
    if not devices:
        await cb.answer("Сначала добавь устройство", show_alert=True)
        return
    await edit(cb, "Для какого устройства нужна ссылка?", kb.pick_device(devices, "gen_link"))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "gen_file"))
async def menu_gen_file(cb: CallbackQuery, client, services):
    devices = await call(services.db.list_devices, client.id)
    if not devices:
        await cb.answer("Сначала добавь устройство", show_alert=True)
        return
    await edit(cb, "Для какого устройства нужен файл?", kb.pick_device(devices, "gen_file"))
    await cb.answer()


@router.callback_query(Menu.filter(F.action == "gen_qr"))
async def menu_gen_qr(cb: CallbackQuery, client, services):
    devices = await call(services.db.list_devices, client.id)
    if not devices:
        await cb.answer("Сначала добавь устройство", show_alert=True)
        return
    await edit(cb, "Для какого устройства нужен QR-код?", kb.pick_device(devices, "gen_qr"))
    await cb.answer()


# ── устройство ───────────────────────────────────────────────────────────────

@router.callback_query(DeviceCB.filter(F.action == "open"))
async def device_open(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    text = texts.device_card_text(dev, for_admin=False)
    if not dev.private_key:
        text += texts.APP_DEVICE_EXPLAIN
    marker = texts.friend_marker(dev)
    if marker:
        text += f"\n\n{marker}"
    await edit(cb, text, kb.device_actions(dev, is_admin=False, back_target=Menu(action="devices").pack()))
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
        return
    old_name = dev.name
    await call(services.rename_device, dev.id, name)
    await message.answer(f"✅ Устройство переименовано: «{old_name}» → «{name}».",
                         reply_markup=kb.reply_hide())


@router.callback_query(DeviceCB.filter(F.action == "connect_menu"))
async def device_connect_menu(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    """«Как планируешь подключить устройство?» — назад к карточке этого же
    устройства."""
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if not dev.private_key:
        # app-устройство: ссылку выдать не можем — дружелюбный диалог
        await edit(cb, texts.APP_DEVICE_PICK_DIALOG, kb.app_device_dialog(dev.id))
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


@router.callback_query(DeviceCB.filter(F.action == "restore"))
async def device_restore_start(cb: CallbackQuery, callback_data: DeviceCB, client, services, state: FSMContext):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await state.set_state(RestoreDevice.link)
    await state.update_data(device_id=dev.id)
    await cb.message.answer(
        "Пришли строку подключения (vpn://…) этого устройства из приложения AmneziaVPN — "
        "включу для него выдачу ссылки и файла через бота.", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(RestoreDevice.link, RoleFilter("client"))
async def device_restore_apply(message: Message, client, services, state: FSMContext):
    link = (message.text or "").strip()
    data = await state.get_data()
    dev = await call(own_device, services, client, data.get("device_id", 0))
    await state.clear()
    if dev is None:
        await message.answer("Устройство не найдено.", reply_markup=kb.reply_hide())
        await _show_main(message, services, client)
        return
    try:
        await call(services.restore_app_device, dev.id, link)
    except ServiceError as e:
        msg = texts.RESTORE_WRONG_DEVICE if str(e) == "WRONG_DEVICE" else str(e)
        await message.answer(msg, reply_markup=kb.reply_hide())
        await _show_main(message, services, client)
        return
    except ValueError:
        await message.answer(texts.RESTORE_BAD_LINK, reply_markup=kb.reply_hide())
        await _show_main(message, services, client)
        return
    try:
        await message.delete()               # vpn:// несёт приватный ключ — убираем из чата
    except Exception:                        # noqa: BLE001
        pass
    await message.answer(f"✅ Устройство «{dev.name}»: управление восстановлено — "
                         "теперь ссылка/QR/файл доступны через бота.",
                         reply_markup=kb.reply_hide())
    await _show_main(message, services, client)


async def _gen_from_menu(cb: CallbackQuery, callback_data, client, services, kind: str):
    """Генерация из меню. Для app-устройства (нет приватного ключа) вместо
    ошибки — дружелюбный диалог «пришли ссылку / удали / назад»."""
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if not dev.private_key:
        await edit(cb, texts.APP_DEVICE_PICK_DIALOG, kb.app_device_dialog(dev.id))
        await cb.answer()
        return
    await drop_message(cb)                           # убрать старое меню (не висеть над ссылкой)
    try:
        await send_device_config(cb.message, services, dev, kind)
    except ServiceError as e:
        await cb.message.answer(str(e))
        await _show_main(cb.message, services, client)
        await cb.answer()
        return
    fin = (texts.finish_link(dev.name) if kind == "link"
           else texts.finish_file(dev.name) if kind == "file"
           else texts.finish_qr(dev.name))
    await content_finisher(cb.message, services, fin, "client")
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "gen_link"))
async def device_gen_link(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    await _gen_from_menu(cb, callback_data, client, services, "link")


@router.callback_query(DeviceCB.filter(F.action == "gen_file"))
async def device_gen_file(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    await _gen_from_menu(cb, callback_data, client, services, "file")


@router.callback_query(DeviceCB.filter(F.action == "gen_qr"))
async def device_gen_qr(cb: CallbackQuery, callback_data: DeviceCB, client, services):
    await _gen_from_menu(cb, callback_data, client, services, "qr")


# ── добавление устройства (FSM: имя) ─────────────────────────────────────────

@router.callback_query(DeviceCB.filter(F.action == "del_menu"))
async def device_del_menu(cb: CallbackQuery, client, services):
    devices = await call(services.db.list_devices, client.id)
    if not devices:
        await cb.answer("Нет устройств", show_alert=True)
        return
    await edit(cb, "Выбери устройство для удаления:", kb.pick_device_to_delete(devices))
    await cb.answer()


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
    await cb.message.answer(
        f"{texts.device_slots_line(used, limit)}\n\nВведи имя нового устройства:",
        reply_markup=kb.reply_cancel())
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "add_friend"))
async def device_add_friend(cb: CallbackQuery, client, services, state: FSMContext):
    used, limit = await call(services.device_slots, client.id)
    await state.set_state(AddDevice.name)
    await state.update_data(for_friend=True)
    await cb.message.answer(
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
        await message.answer(f"✅ Устройство «{texts._e(name)}» создано для друга.",
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
                    kb.connect_method_choice(dev.id, back))


# ── удаление (усиленное для единственного) ──────────────────────────────────

async def _show_delete_prompt(cb, services, dev):
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
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await _show_delete_prompt(cb, services, dev)
    await cb.answer()


@router.callback_query(DelDeviceCB.filter(F.stage == "confirm"))
async def device_delete_confirm(cb: CallbackQuery, callback_data: DelDeviceCB, client, services):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    try:
        await remove_device_and_notify(cb.bot, services, dev.id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    devices = await call(services.db.list_devices, client.id)
    slots = await call(services.device_slots, client.id)
    await edit(cb, "🗑 Устройство удалено.\n\n<b>📱Твои устройства</b>\n\n" + texts.device_slots_line(*slots),
               kb.client_devices(devices))
    await cb.answer()


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
    ok, new_end = await call(services.activate_grace, client.id, settings.get_int("grace.grace_days", 14))
    if not ok:
        await cb.answer(texts.GRACE_STALE, show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    # гасим кнопки у уведомления и подтверждаем
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(
        texts.grace_activated_client(settings.get_int("grace.grace_days", 14), timeutil.fmt_dt(new_end)))
    if settings.get_bool("notifications.client_events.grace", True):
        await notify_one(cb.message.bot, config.ADMIN_ID,
                         texts.grace_activated_admin(client.name, settings.get_int("grace.grace_days", 14)))
    await cb.answer("Продлено")

# ── Ручная блокировка своего устройства (клиент) ─────────────────────────────
# Клиент ставит/снимает USER-бит на СВОИХ устройствах (в т.ч. friend — они его).
# Всегда «громко»: friend-устройство → друг получает уведомление. Тихого варианта
# у клиента нет. Админские биты клиент не трогает (их снимает только админ).

from awgbot.core.blocks import DeviceBlock as DeviceBlock


@router.callback_query(BlockCB.filter(F.action == "menu_block"))
async def client_block_device(cb: CallbackQuery, callback_data: BlockCB, client, services):
    if callback_data.target != "dev":
        await cb.answer("Недоступно", show_alert=True)
        return
    dev = await call(own_device, services, client, callback_data.ref)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    notes = await call(services.block_device_manual, dev.id, DeviceBlock.USER, True)
    await send_notifications(cb.bot, notes)
    dev = await call(services.db.get_device, dev.id)
    await edit(cb, texts.device_card_text(dev, for_admin=False),
               kb.device_actions(dev, is_admin=False, back_target=Menu(action="devices").pack()))
    await cb.answer("Заблокировано")


@router.callback_query(BlockCB.filter(F.action == "menu_unblock"))
async def client_unblock_device(cb: CallbackQuery, callback_data: BlockCB, client, services):
    """Клиент снимает ТОЛЬКО свой USER-бит. Админские биты не трогает — если
    устройство заблокировано и админом, оно останется заблокированным."""
    if callback_data.target != "dev":
        await cb.answer("Недоступно", show_alert=True)
        return
    dev = await call(own_device, services, client, callback_data.ref)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if not (int(dev.block_reason) & int(DeviceBlock.USER)):
        await cb.answer("Ты не блокировал это устройство", show_alert=True)
        return
    notes = await call(services.unblock_device_manual, dev.id, DeviceBlock.USER, True)
    await send_notifications(cb.bot, notes)
    dev = await call(services.db.get_device, dev.id)
    await edit(cb, texts.device_card_text(dev, for_admin=False),
               kb.device_actions(dev, is_admin=False, back_target=Menu(action="devices").pack()))
    await cb.answer("Разблокировано")

# ── Приостановка подписки («в отпуск») ───────────────────────────────────────

async def _show_info(cb, client, services):
    """Перерисовать «Управлять подпиской» (после входа/выхода из паузы)."""
    client = await call(services.db.get_client, client.id)
    devices = await call(services.db.list_devices, client.id)
    traffic = await call(services.db.get_client_traffic, client.id)
    online = await call(services.client_is_online, client.id)
    paused_user, can_pause = _pause_flags(client)
    await edit(cb, texts.subscription_manage_text(client, traffic, online, len(devices)),
               kb.client_info_actions(client, paused=paused_user, can_pause=can_pause))


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
    await edit(cb, texts.pause_ask(avail, int(client.pause_used_days),
                                   settings.get_int("pause.pause_max_total_days", 28)),
               kb.pause_day_choice(client.id, avail))
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
    await message.answer(texts.pause_warning(days), reply_markup=kb.pause_confirm(client.id, days))


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
    if config.EMAIL_RESUME_ENABLED and code:
        # итог — без кнопки (остаётся в чате как запись); кнопка «В меню» — на
        # аварийном сообщении ниже (оно последнее и становится нав-сообщением).
        await edit(cb, summary, None)
        sent = await cb.message.answer(
            texts.pause_emergency_code(code, config.EMAIL_RESUME_ADDRESS),
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
    await cb.message.answer(texts.pause_resumed_self(actual, new_end),
                            reply_markup=kb.hide_only())
    await cb.answer("Возобновлено")
    await _show_info(cb, client, services)


@router.callback_query(PauseCB.filter(F.action == "cancel"))
async def pause_cancel(cb: CallbackQuery, callback_data: PauseCB, client, services):
    await _show_info(cb, client, services)
    await cb.answer()


__all__ = ["router"]
