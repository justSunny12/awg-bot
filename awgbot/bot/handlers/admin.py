"""
handlers/admin.py — роутер администратора.

Управление клиентами (создание с инвайтом, продление с остатком, редактирование,
удаление), выдача конфигов, статус сервера, бэкап, перезапуск сервиса, работа с
устройствами без клиента (привязка, реставрация).
"""

from __future__ import annotations

import time

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.util import timeutil
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import (AdminLinkGate, FaHintCB, AdminSelfCB, BlockCB, ClientCB, ConfirmCB, DelDeviceCB, DeviceCB,
                       Menu, PeriodCB, ReassignCB, UpdateCB, BroadcastCB)
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import (call, edit, edit_nav, ask_tracked, drop_message,
                             remove_device_and_notify, send_conf, cleanup_content,
                             send_link, send_qr, send_menu, content_finisher,
                             show_main_menu)
from awgbot.bot.notifier import notify_one, send_notifications, broadcast
from awgbot.domain.services import BYTES_PER_GB, SECONDS_PER_DAY, LimitReached, ServiceError
from awgbot.bot.states import (AdminAddDevice, AdminSelfAddDevice, BlockPauseDays, Broadcast, CreateClient,
                    EditLimit, EditName, EditDeviceName, EditPeriod, EditTrafficLimit, RestoreDevice)

router = Router(name="admin")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))


async def _main_menu_markup(services):
    n = await call(services.count_unassigned_app_devices)
    ac = await call(services.admin_client)
    has_dev = bool(ac and await call(services.db.count_devices, ac.id))
    return kb.admin_main(n, self_has_devices=has_dev)


async def _delete_user_link(message) -> None:
    """Удалить сообщение пользователя с присланной vpn:// ссылкой из чата.
    Ссылка — секрет (root-доступ к серверу или приватный ключ устройства), в
    истории Telegram ей не место. Тихо игнорируем, если удалить нельзя
    (>48 ч, нет прав, уже удалено)."""
    try:
        await message.delete()
    except Exception:                            # noqa: BLE001
        pass


async def _return_panel(message, services) -> None:
    """Показать админ-панель новым сообщением — единый «выход» из любого диалога,
    чтобы юзер не оставался без навигации. Гасит прежнее активное меню и стирает
    промежуточные служебные сообщения диалога (вопросы, ввод, ссылки)."""
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, await _panel_text(services),
                    await _main_menu_markup(services))


async def _panel_text(services) -> str:
    """Шапка панели: статус из кэша (0 docker exec, мгновенно)."""
    st = await call(services.server_status_cached)
    tot = await call(services.db.get_total_month_traffic)
    st = {**st, "traffic_rx": tot["rx"], "traffic_tx": tot["tx"]}
    return texts.admin_panel(st)


# ─────────────────────────────────────────────────────────────────────────────
# /start и главное меню
# ─────────────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def admin_start(message: Message, services, state: FSMContext):
    await state.clear()
    await _return_panel(message, services)


@router.callback_query(Menu.filter(F.action == "main"))
async def admin_main_menu(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    await edit_nav(cb, services, await _panel_text(services), await _main_menu_markup(services))
    await cb.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Клиенты: список / карточка
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "clients"))
async def clients_list(cb: CallbackQuery, services):
    clients = await call(services.db.list_clients, admin_first_tg=config.ADMIN_ID)  # админ первым, включая его профиль
    if not clients:
        await edit_nav(cb, services, "Профилей пока нет.", await _main_menu_markup(services))
    else:
        await edit(cb, "👥 Профили:", kb.admin_clients(clients))
    await cb.answer()


async def _show_client_card(cb: CallbackQuery, services, client_id: int):
    client = await call(services.db.get_client, client_id)
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    devices = await call(services.db.list_devices, client_id)
    traffic = await call(services.db.get_client_traffic, client_id)
    online = await call(services.client_is_online, client_id)
    text = texts.client_card(client, devices, traffic, online, for_admin=True)
    is_admin_owner = client.tg_id == config.ADMIN_ID
    await edit(cb, text, kb.admin_client_actions(
        client, has_devices=bool(devices), is_admin_owner=is_admin_owner))


@router.callback_query(ClientCB.filter(F.action == "open"))
async def client_open(cb: CallbackQuery, callback_data: ClientCB, services):
    await _show_client_card(cb, services, callback_data.client_id)
    await cb.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Создание клиента (FSM: имя → лимит → период)
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "add_client"))
async def add_client_start(cb: CallbackQuery, services, state: FSMContext):
    await state.set_state(CreateClient.name)
    await ask_tracked(cb.message, services, "Введи имя нового профиля:", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(CreateClient.name)
async def add_client_name(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, "Имя не может быть пустым. Введи ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(CreateClient.limit)
    await ask_tracked(message, services, "Сколько устройств разрешить профилю? Число, например 3 (0 — без ограничения)", reply_markup=kb.reply_cancel())


@router.message(CreateClient.limit)
async def add_client_limit(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, "Введи число (0 — без ограничения):")
        return
    await state.update_data(limit=int(raw))
    await state.set_state(CreateClient.traffic)
    await ask_tracked(message, services, texts.TRAFFIC_LIMIT_CLIENT_ASK, reply_markup=kb.reply_cancel())


@router.message(CreateClient.traffic)
async def add_client_traffic(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.TRAFFIC_LIMIT_BAD)
        return
    await state.update_data(traffic_gb=int(raw))
    # снимаем реплай-«Отмена» (текстовый ввод закончен) — иначе виснет поверх
    # инлайн-экрана выбора периода. «Принято» неинформативно — трекаем на удаление.
    _accepted = await message.answer("Принято.", reply_markup=kb.reply_hide())
    await call(services.db.add_content_msg_id, _accepted.chat.id, _accepted.message_id)
    # выбор периода — промежуточный шаг; трекаем, чтобы content_finisher
    # убрал его при возврате в меню (вместе с вводом пользователя).
    _period = await message.answer("Выбери срок подписки:", reply_markup=kb.period_choices("create"))
    await call(services.db.add_content_msg_id, _period.chat.id, _period.message_id)


@router.callback_query(PeriodCB.filter(F.ctx == "create"))
async def add_client_period(cb: CallbackQuery, callback_data: PeriodCB, services, state: FSMContext):
    data = await state.get_data()
    name = data.get("name")
    limit = data.get("limit")
    traffic_gb = data.get("traffic_gb")
    await state.clear()
    if not name or limit is None or traffic_gb is None:  # limit/traffic=0 валидны
        # Протухший диалог (рестарт бота / старые кнопки): гасим ЭТИ кнопки и
        # возвращаем в панель — юзер не остаётся с мёртвым выбором периода.
        await cb.answer("Диалог устарел — открой создание профиля заново", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _return_panel(cb.message, services)
        return
    try:
        created = await call(services.create_client, name, limit, callback_data.kind,
                             traffic_gb * BYTES_PER_GB)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    # получаем username бота для ссылки
    me = await cb.bot.me()
    link = f"https://t.me/{me.username}?start={created.invite_code}"
    # ссылка-приглашение — транзиентная (переслал и забыл): трекаем на удаление
    sent_link = await cb.message.answer(texts.INVITE_FORWARD_TEMPLATE.format(link=link))
    await call(services.db.add_content_msg_id, sent_link.chat.id, sent_link.message_id)
    # финишер — констатирующий РЕЗУЛЬТАТ (остаётся): что за профиль создан.
    report = texts.client_created_report(
        name, device_limit=limit,
        traffic_limit_bytes=traffic_gb * BYTES_PER_GB,
        period_kind=callback_data.kind, period_end=created.period_end)
    await content_finisher(cb.message, services, report, "admin")
    await cb.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Редактирование клиента: имя / лимит
# ─────────────────────────────────────────────────────────────────────────────

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
    # уведомляем клиента — тем же принципом, что при переназначении устройства
    if client and client.tg_id:
        used, limit = await call(services.device_slots, client_id)
        await notify_one(
            message.bot, client.tg_id,
            texts.reassign_recipient_notice(name, used, limit,
                                           recipient_is_admin=(client.tg_id == config.ADMIN_ID)),
            reply_markup=kb.added_by_admin(created.device_id))
    await _return_panel(message, services)


@router.callback_query(ClientCB.filter(F.action == "edit_name"))
async def edit_name_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    await state.set_state(EditName.value)
    await state.update_data(client_id=callback_data.client_id)
    await ask_tracked(cb.message, services, "Введи новое имя профиля:", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(EditName.value)
async def edit_name_apply(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, "Имя не может быть пустым:")
        return
    data = await state.get_data()
    await state.clear()
    old = await call(services.db.get_client, data["client_id"])
    old_name = old.name if old else "?"
    await call(services.db.update_client_fields, data["client_id"], name=name)
    await message.answer(f"✅ Профиль переименован: «{old_name}» → «{name}».",
                         reply_markup=kb.reply_hide())
    await _return_panel(message, services)


@router.callback_query(ClientCB.filter(F.action == "edit_limit"))
async def edit_limit_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    await state.set_state(EditLimit.value)
    await state.update_data(client_id=callback_data.client_id)
    await ask_tracked(cb.message, services, "Введи новый лимит устройств. Число (0 — без ограничения):", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(EditLimit.value)
async def edit_limit_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, "Введи число (0 — без ограничения):")
        return
    data = await state.get_data()
    client_id = data["client_id"]
    new_limit = int(raw)
    client = await call(services.db.get_client, client_id)
    if client is None:
        await state.clear()
        await message.answer("Профиль не найден.")
        return
    count = await call(services.db.count_devices, client_id)
    # Понижение лимита НИЖЕ текущего числа устройств: предупреждаем и спрашиваем.
    # Не блокируем (админ вправе «заморозить» добавление), но честно показываем
    # последствие, чтобы не создавать «3 из 2» вслепую.
    if new_limit != 0 and new_limit < count:
        await state.update_data(pending_limit=new_limit)
        _acc = await message.answer("Принято.", reply_markup=kb.reply_hide())
        await call(services.db.add_content_msg_id, _acc.chat.id, _acc.message_id)
        await message.answer(
            f"У профиля сейчас {count} "
            f"{texts.plural_ru(count, 'устройство', 'устройства', 'устройств')}, "
            f"а ты выставляешь лимит {new_limit}.\n\n"
            "Существующие устройства продолжат работать, но добавить новые "
            f"профиль не сможет, пока их не станет меньше {new_limit}. "
            "Отображаться будет как превышение (например «3 из 2»).\n\n"
            "Применить такой лимит?",
            reply_markup=kb.confirm_lower_limit())
        return
    await state.clear()
    await _apply_limit(message, services, client, new_limit)


async def _apply_limit(message, services, client, new_limit: int):
    old_limit = client.device_limit
    await call(services.db.update_client_fields, client.id, device_limit=new_limit)
    old_s = "без ограничения" if not old_limit else str(old_limit)
    new_s = "без ограничения" if not new_limit else str(new_limit)
    await message.answer(
        f"✅ Профиль «{client.name}»: лимит устройств изменён {old_s} → {new_s}.",
        reply_markup=kb.reply_hide())
    if client.tg_id and old_limit != new_limit:
        await notify_one(message.bot, client.tg_id,
                         texts.limit_changed_notice(old_limit, new_limit))
    await _return_panel(message, services)


@router.callback_query(ConfirmCB.filter(F.action == "lower_limit"))
async def edit_limit_confirm(cb: CallbackQuery, callback_data: ConfirmCB, services, state: FSMContext):
    data = await state.get_data()
    client_id = data.get("client_id")
    new_limit = data.get("pending_limit")
    await state.clear()
    if not callback_data.yes:
        await edit_nav(cb, services, "Отменено — лимит не изменён.", await _main_menu_markup(services))
        await cb.answer()
        return
    client = await call(services.db.get_client, client_id)
    if client is None or new_limit is None:
        await cb.answer("Диалог устарел, начни заново", show_alert=True)
        return
    await _apply_limit(cb.message, services, client, new_limit)
    await edit_nav(cb, services, "Готово.", await _main_menu_markup(services))
    await cb.answer()


# ── Редактирование лимита потребления (админ: клиент-тотал и устройство) ──────

@router.callback_query(ClientCB.filter(F.action == "edit_traffic"))
async def edit_client_traffic_start(cb: CallbackQuery, callback_data: ClientCB,
                                    services, state: FSMContext):
    await state.set_state(EditTrafficLimit.value)
    await state.update_data(kind="client", ref=callback_data.client_id)
    await ask_tracked(cb.message, services, texts.TRAFFIC_LIMIT_CLIENT_ASK, reply_markup=kb.reply_cancel())
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "edit_traffic"))
async def edit_device_traffic_start(cb: CallbackQuery, callback_data: DeviceCB,
                                    state: FSMContext, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    await state.set_state(EditTrafficLimit.value)
    await state.update_data(kind="device", ref=callback_data.device_id)
    plimit = await call(services.profile_traffic_limit, dev.client_id) if dev else 0
    await ask_tracked(cb.message, services, texts.traffic_limit_device_ask(plimit), reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(EditTrafficLimit.value)
async def edit_traffic_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.TRAFFIC_LIMIT_BAD)
        return
    data = await state.get_data()
    kind = data.get("kind")
    ref = data.get("ref")
    await state.clear()
    limit_bytes = int(raw) * BYTES_PER_GB
    if kind == "client":
        cl = await call(services.db.get_client, ref)
        old_b = int(cl.traffic_limit) if cl else 0
        await call(services.set_client_traffic_limit, ref, limit_bytes)
        old_s = "без ограничения" if not old_b else texts.gb_str(old_b)
        new_s = "без ограничения" if not limit_bytes else texts.gb_str(limit_bytes)
        cname = cl.name if cl else "?"
        await message.answer(
            f"✅ Профиль «{cname}»: лимит потребления {old_s} → {new_s}.",
            reply_markup=kb.reply_hide())
    elif kind == "device":
        dev = await call(services.db.get_device, ref)
        old_b = int(dev.traffic_limit) if dev else 0
        await call(services.set_device_traffic_limit, ref, limit_bytes)
        old_s = "без ограничения" if not old_b else texts.gb_str(old_b)
        new_s = "без ограничения" if not limit_bytes else texts.gb_str(limit_bytes)
        cl = await call(services.db.get_client, dev.client_id) if dev else None
        cname = cl.name if cl else "?"
        await message.answer(
            f"✅ Устройство «{dev.name if dev else '?'}» (профиль «{cname}»): "
            f"лимит потребления {old_s} → {new_s}.",
            reply_markup=kb.reply_hide())
    await _return_panel(message, services)


# ─────────────────────────────────────────────────────────────────────────────
# Перевыпуск инвайта / удаление клиента
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "regen_invite"))
async def regen_invite(cb: CallbackQuery, callback_data: ClientCB, services):
    try:
        code = await call(services.regenerate_invite, callback_data.client_id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    me = await cb.bot.me()
    link = f"https://t.me/{me.username}?start={code}"
    sent = await cb.message.answer(texts.INVITE_FORWARD_TEMPLATE.format(link=link))
    await call(services.db.add_content_msg_id, sent.chat.id, sent.message_id)
    await content_finisher(cb.message, services, texts.FINISH_CLIENT_INVITE, "admin")
    await cb.answer("Новый инвайт создан")


@router.callback_query(ClientCB.filter(F.action == "delete"))
async def client_delete_confirm(cb: CallbackQuery, callback_data: ClientCB, services):
    client = await call(services.db.get_client, callback_data.client_id)
    name = client.name if client else "?"
    await edit(cb, f"Удалить профиль «{texts._e(name)}» вместе со всеми его устройствами?",
               kb.yes_no("del_client", ref=callback_data.client_id))
    await cb.answer()


@router.callback_query(ConfirmCB.filter(F.action == "del_client"))
async def client_delete_apply(cb: CallbackQuery, callback_data: ConfirmCB, services):
    if not callback_data.yes:
        await _show_client_card(cb, services, callback_data.ref)
        await cb.answer("Отменено")
        return
    # снять устройства с сервера, затем удалить клиента (каскад в БД).
    # remove_device_and_notify: друзья переданных устройств получают
    target = await call(services.db.get_client, callback_data.ref)
    if target is not None and target.tg_id == config.ADMIN_ID:
        await cb.answer("Профиль администратора нельзя удалить", show_alert=True)
        return
    # уведомление, что доступ прекращён (просто remove_device его терял).
    _victim = await call(services.db.get_client, callback_data.ref)
    _vname = _victim.name if _victim else "?"
    devices = await call(services.db.list_devices, callback_data.ref)
    _dcount = len(devices)
    for d in devices:
        try:
            await remove_device_and_notify(cb.bot, services, d.id)
        except ServiceError:
            pass
    await call(services.db.delete_client, callback_data.ref)
    await edit_nav(cb, services,
                   f"🗑 Профиль «{_vname}» удалён (устройств удалено: {_dcount}).",
                   await _main_menu_markup(services))
    await cb.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Продление (период → при остатке спрашиваем сохранение)
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "extend"))
async def extend_start(cb: CallbackQuery, callback_data: ClientCB, services):
    client = await call(services.db.get_client, callback_data.client_id)
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    cut_days = int(client.grace_pending_cut) // SECONDS_PER_DAY
    text = "На какой срок продлить?"
    if cut_days > 0:
        text = (f"⚠️ Профиль брал отсрочку на {cut_days} дн. — она вычтется из "
                f"нового периода. Доступны только периоды длиннее {cut_days} дн.\n\n"
                "На какой срок продлить?")
    await edit(cb, text, kb.period_choices("extend", ref=callback_data.client_id,
                                            min_days=cut_days))
    await cb.answer()


@router.callback_query(PeriodCB.filter(F.ctx == "extend"))
async def extend_period_chosen(cb: CallbackQuery, callback_data: PeriodCB, services, state: FSMContext):
    client_id = callback_data.ref
    remainder = await call(services.remaining_for, client_id)
    # для «Бессрочно» вопрос об остатке бессмыслен (прибавлять некуда) — сразу
    if remainder > 0 and callback_data.kind != "never":
        # спросим про сохранение остатка; период запомним в FSM
        await state.update_data(extend_kind=callback_data.kind, extend_client=client_id)
        await edit(cb, texts.EXTEND_KEEP_QUESTION.format(
            remainder=timeutil.fmt_remaining_short(remainder)),
            kb.yes_no("keep", ref=client_id))
    else:
        await _do_extend(cb, services, client_id, callback_data.kind, keep=False)
    await cb.answer()


@router.callback_query(ConfirmCB.filter(F.action == "keep"))
async def extend_keep_answer(cb: CallbackQuery, callback_data: ConfirmCB, services, state: FSMContext):
    data = await state.get_data()
    kind = data.get("extend_kind")
    client_id = data.get("extend_client") or callback_data.ref
    await state.clear()
    if not kind:
        await cb.answer("Диалог прерван, начни заново", show_alert=True)
        return
    await _do_extend(cb, services, client_id, kind, keep=callback_data.yes)
    await cb.answer()


async def _do_extend(cb, services, client_id, kind, keep: bool):
    try:
        result = await call(services.extend_period, client_id, kind, keep)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await send_notifications(cb.bot, result.notifications)
    done = ("✅ Подписка теперь бессрочная." if result.new_end is None
            else f"✅ Подписка продлена до {timeutil.fmt_dt(result.new_end)}.")
    await edit_nav(cb, services, done, await _main_menu_markup(services))


# ── Изменить период вручную (лечит дедлок бессрочной подписки) ────────────────

@router.callback_query(ClientCB.filter(F.action == "edit_period"))
async def edit_period_start(cb: CallbackQuery, callback_data: ClientCB,
                            services, state: FSMContext):
    client = await call(services.db.get_client, callback_data.client_id)
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    cur_start = client.period_start
    cur_txt = timeutil.fmt_dt_sec(timeutil.parse_iso(cur_start)) if cur_start else "—"
    await state.set_state(EditPeriod.start)
    await state.update_data(client_id=client.id)
    await ask_tracked(cb.message, services,
        f"Выбери новую дату начала подписки (или отправь «-», чтобы оставить "
        f"текущую: {cur_txt})\nФормат ввода: DD.MM.YYYY HH:MM:SS",
        reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(EditPeriod.start)
async def edit_period_start_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    data = await state.get_data()
    client = await call(services.db.get_client, data["client_id"])
    if client is None:
        await state.clear()
        await message.answer("Профиль не найден.", reply_markup=kb.reply_hide())
        return
    if raw == "-":
        # оставить текущую дату начала
        new_start = client.period_start and timeutil.parse_iso(client.period_start)
        if new_start is None:
            await ask_tracked(message, services, "У профиля нет текущей даты начала — нельзя оставить "
                                 "«как есть». Введи дату (DD.MM.YYYY HH:MM:SS):",
                                 reply_markup=kb.reply_cancel())
            return
    else:
        try:
            new_start = timeutil.parse_dt_sec(raw)
        except ValueError:
            await ask_tracked(message, services, "Не разобрал дату. Формат: DD.MM.YYYY HH:MM:SS. "
                                 "Попробуй ещё раз (или «-» — оставить текущую):",
                                 reply_markup=kb.reply_cancel())
            return
    await state.update_data(new_start=timeutil.to_iso(new_start))
    cur_end = client.period_end
    cur_txt = timeutil.fmt_dt_sec(timeutil.parse_iso(cur_end)) if cur_end else "бессрочно"
    await state.set_state(EditPeriod.end)
    await ask_tracked(message, services,
        f"Выбери новую дату окончания подписки (текущая: {cur_txt}).\n"
        f"«-» — оставить как есть, «0» — сделать бессрочной.\n"
        f"Формат ввода: DD.MM.YYYY HH:MM:SS",
        reply_markup=kb.reply_cancel())


@router.message(EditPeriod.end)
async def edit_period_end_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    data = await state.get_data()
    client = await call(services.db.get_client, data["client_id"])
    if client is None:
        await state.clear()
        await message.answer("Профиль не найден.", reply_markup=kb.reply_hide())
        return
    # семантика: «-» оставить текущую (может быть None=бессрочно), «0» → бессрочно,
    # иначе — распарсить дату. Различаем «оставить» и «сделать бессрочным» флагом.
    if raw == "-":
        new_end = client.period_end and timeutil.parse_iso(client.period_end)  # None если уже бессрочно
    elif raw == "0":
        new_end = None                       # сделать бессрочной
    else:
        try:
            new_end = timeutil.parse_dt_sec(raw)
        except ValueError:
            await ask_tracked(message, services, "Не разобрал дату. Формат: DD.MM.YYYY HH:MM:SS. "
                                 "«-» — оставить, «0» — бессрочно. Попробуй ещё раз:",
                                 reply_markup=kb.reply_cancel())
            return
    await state.clear()
    saved_start = data.get("new_start")
    new_start = timeutil.parse_iso(saved_start) if saved_start else None
    if new_start is None:
        await message.answer("Дата начала не задана — начни заново.",
                             reply_markup=kb.reply_hide())
        await _return_panel(message, services)
        return
    try:
        s, e, notes = await call(services.set_subscription_dates, client.id, new_start, new_end)
    except ServiceError as ex:
        await message.answer(str(ex), reply_markup=kb.reply_hide())
        await _return_panel(message, services)
        return
    await send_notifications(message.bot, notes)
    end_txt = timeutil.fmt_dt_sec(e) if e else "бессрочно"
    await message.answer(
        f"Период подписки профиля {client.name} успешно изменён.\n"
        f"Новый период: {timeutil.fmt_dt_sec(s)} - {end_txt}",
        reply_markup=kb.reply_hide())
    await _return_panel(message, services)


# ── Админ выводит клиента из приостановки («в отпуск») ───────────────────────

@router.callback_query(ClientCB.filter(F.action == "resume_pause"))
async def admin_resume_pause(cb: CallbackQuery, callback_data: ClientCB, services):
    """Ручной вывод клиента из клиентской паузы. Тот же exit_pause, что у клиента:
    списывает фактические дни (ceil), возвращает неиспользованный остаток в
    period_end, снимает PAUSED-каскад с устройств. Запасной выход из deadlock,
    когда клиент заперся в паузе (Telegram только через этот VPN)."""
    client = await call(services.db.get_client, callback_data.client_id)
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    ok, actual, new_end, notes = await call(services.exit_pause, client.id, auto=False)
    if not ok:
        await cb.answer("Профиль не на паузе", show_alert=True)
        await _show_client_card(cb, services, client.id)
        return
    await send_notifications(cb.bot, notes)
    end_txt = timeutil.fmt_dt(new_end) if new_end else "бессрочно"
    await cb.message.answer(
        f"▶️ Профиль «{client.name}» выведен из приостановки.\n"
        f"Списано дней: {actual}. Новый срок: {end_txt}.",
        reply_markup=kb.reply_hide())
    await cb.answer("Возобновлено")
    await _show_client_card(cb, services, client.id)


# ─────────────────────────────────────────────────────────────────────────────
# Выдача конфига клиента (админ) — выбор устройства
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "gen_for"))
async def admin_gen_for(cb: CallbackQuery, callback_data: ClientCB, services):
    devices = await call(services.db.list_devices, callback_data.client_id)
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
@router.callback_query(DeviceCB.filter(F.action == "gen_link"))
async def admin_dev_link(cb: CallbackQuery, callback_data: DeviceCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if getattr(dev, "is_admin", False):
        await edit(cb, texts.ADMIN_LINK_WARNING, kb.admin_link_gate(dev.id, "link"))
        await cb.answer()
        return
    try:
        cfg = await call(services.generate_config, dev.id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await drop_message(cb)                           # убрать «Как подключить» — не висеть над ссылкой
    await send_link(cb.message, cfg["vpn"], services)
    await content_finisher(cb.message, services, texts.finish_link(dev.name), "admin")
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "gen_qr"))
async def admin_dev_qr(cb: CallbackQuery, callback_data: DeviceCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if getattr(dev, "is_admin", False):
        await edit(cb, texts.ADMIN_LINK_WARNING, kb.admin_link_gate(dev.id, "qr"))
        await cb.answer()
        return
    try:
        cfg = await call(services.generate_config, dev.id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await drop_message(cb)                           # убрать «Как подключить» — не висеть над ссылкой
    await send_qr(cb.message, cfg["vpn"], services)
    await content_finisher(cb.message, services, texts.finish_qr(dev.name), "admin")
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "gen_file"))
async def admin_dev_file(cb: CallbackQuery, callback_data: DeviceCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if getattr(dev, "is_admin", False):
        await edit(cb, texts.ADMIN_LINK_WARNING, kb.admin_link_gate(dev.id, "file"))
        await cb.answer()
        return
    try:
        cfg = await call(services.generate_config, dev.id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await drop_message(cb)                           # убрать «Как подключить» — не висеть над ссылкой
    await send_conf(cb.message, dev.name, cfg["conf"], services)
    await content_finisher(cb.message, services, texts.finish_file(dev.name), "admin")
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
    back_target, reassign_label = await _device_back_target_and_label(services, dev)
    markup = kb.device_actions(dev, is_admin=True, back_target=back_target,
                               reassign_label=reassign_label)
    text = texts.device_card_text(dev, for_admin=True)
    marker = texts.friend_marker(dev)
    if marker:
        text += f"\n\n{marker}"
    await edit(cb, text, markup)
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "connect_menu"))
async def admin_device_connect_menu(cb: CallbackQuery, callback_data: DeviceCB, services):
    """«Как планируешь подключить устройство?» — назад к карточке этого же
    устройства."""
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if getattr(dev, "is_admin", False):
        # ФА-устройство: не «конфиг для подключения», а выдача сохранённой
        # ссылки полного доступа (QR/ссылка/файл) — каждый способ через гейт.
        back = DeviceCB(action="open", device_id=dev.id).pack()
        await edit(cb, texts.CONNECT_METHOD_ASK, kb.connect_method_choice(dev.id, back))
        await cb.answer()
        return
    if not dev.private_key:
        # app-устройство: ссылки нет — диалог реставрации/удаления
        await edit(cb, texts.APP_DEVICE_PICK_DIALOG, kb.app_device_dialog(dev.id))
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
    await cb.answer()


@router.callback_query(FaHintCB.filter(F.action == "ignore"))
async def fa_hint_ignore(cb: CallbackQuery, services):
    await call(services.dismiss_admin_fa_hint)
    await edit(cb, "Ок, больше не напоминаю. Назначить доступ можно позже — "
                   "через устройство без профиля.", None)
    await cb.answer()


@router.callback_query(FaHintCB.filter(F.action == "choose"))
async def fa_hint_choose(cb: CallbackQuery, services):
    """Показать устройства без профиля для назначения полного доступа. Имя берём
    из clientsTable (там full-access значится как «Admin [платформа]»), рядом —
    IP в квадратных скобках, чтобы админ уверенно опознал нужное устройство."""
    service_id = await call(services.db.get_service_client_id)
    devices = await call(services.db.list_devices, service_id)
    if not devices:
        await edit(cb, "Устройств без профиля нет — назначить нечего.", None)
        await cb.answer()
        return
    names = await call(services.clientstable_names)
    await edit(cb, texts.ADMIN_FA_PICK, kb.fa_pick_devices(devices, names))
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "fa_assign"))
async def fa_assign_confirm(cb: CallbackQuery, callback_data: DeviceCB, services):
    """Шаг подтверждения перед назначением ФА: показываем имя+IP выбранного устройства (было: пира)
    пира и мягко предупреждаем, если имя не похоже на «Admin […]»."""
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    names = await call(services.clientstable_names)
    ct = names.get(dev.public_key) or dev.name
    looks_admin = ct.strip().startswith("Admin [")
    await edit(cb, texts.fa_assign_confirm(ct, dev.address, looks_admin),
               kb.fa_assign_confirm(dev.id))
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


@router.callback_query(DeviceCB.filter(F.action == "clear_fa"))
async def fa_clear_start(cb: CallbackQuery, callback_data: DeviceCB):
    await edit(cb, texts.ADMIN_FA_CLEAR_WARNING, kb.confirm_clear_fa(callback_data.device_id))
    await cb.answer()


@router.callback_query(AdminLinkGate.filter((F.method == "clear") & (F.confirm == True)))  # noqa: E712
async def fa_clear_confirmed(cb: CallbackQuery, callback_data: AdminLinkGate, services):
    """Подтверждено снятие метки → стираем ссылку, устройство → в общий пул."""
    _dev = await call(services.db.get_device, callback_data.device_id)
    _dname = _dev.name if _dev else "?"
    try:
        await call(services.clear_full_access, callback_data.device_id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await edit(cb, f"✅ Устройство «{_dname}»: метка полного доступа снята, ссылка "
                   "удалена. Устройство вернулось в список без профиля.", None)
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "fa_link"))
async def fa_link_ask(cb: CallbackQuery, callback_data: DeviceCB, state: FSMContext):
    """Назначение ФА подтверждено → просим
    ссылку полного доступа. mode=fa → attach_full_access."""
    await state.set_state(RestoreDevice.link)
    await state.update_data(device_id=callback_data.device_id, mode="fa")
    await cb.message.answer(
        "Пришли ссылку полного доступа (vpn://…) к серверу из приложения Amnezia.",
        reply_markup=kb.reply_cancel())
    await cb.answer()


@router.callback_query(DeviceCB.filter(F.action == "restore"))
async def device_restore_start(cb: CallbackQuery, callback_data: DeviceCB,
                               state: FSMContext, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    # Замена ссылки у уже назначенного full-access устройства: прежняя ссылка
    # утрачивается безвозвратно — требуем явного подтверждения.
    if dev is not None and getattr(dev, "is_admin", False):
        await edit(cb, texts.ADMIN_FA_CHANGE_WARNING,
                   kb.confirm_change_fa_link(dev.id))
        await cb.answer()
        return
    # обычное app-устройство: клиентская реставрация
    await state.set_state(RestoreDevice.link)
    await state.update_data(device_id=callback_data.device_id, mode="client")
    await cb.message.answer(
        "Пришли строку подключения (vpn://…) этого устройства из приложения "
        "Amnezia — включу полный доступ по нему.",
        reply_markup=kb.reply_cancel())
    await cb.answer()


@router.callback_query(AdminLinkGate.filter((F.method == "change") & (F.confirm == True)))  # noqa: E712
async def fa_change_confirmed(cb: CallbackQuery, callback_data: AdminLinkGate,
                              state: FSMContext):
    """Подтверждено «заменить ссылку» → просим новую ФА-ссылку (mode=fa)."""
    await state.set_state(RestoreDevice.link)
    await state.update_data(device_id=callback_data.device_id, mode="fa")
    await cb.message.answer(
        "Пришли НОВУЮ ссылку полного доступа (vpn://…) — прежняя будет заменена.",
        reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(RestoreDevice.link)
async def device_restore_apply(message: Message, services, state: FSMContext):
    """Единый приём vpn:// для реставрации/ФА, ветвление под капотом.
    mode: client — клиентская реставрация app-устройства; fa — прикрепление ФА;
    fa_transfer — подтверждённый перенос ФА на другое устройство.
    Если в mode=client пришла ФА-ссылка (юзер прислал не ту) — не отказываем,
    а прозрачно перенаправляем в ФА-путь (единый вход, ветвление по типу)."""
    link = (message.text or "").strip()
    data = await state.get_data()
    dev_id = data["device_id"]
    mode = data.get("mode", "client")
    await state.clear()

    async def _attach(transfer: bool):
        """ФА-путь: attach_full_access + обработка инвариантов. Возвращает True,
        если диалог продолжается (перенос) — тогда наверху делать ничего."""
        try:
            await call(services.attach_full_access, dev_id, link, transfer=transfer)
        except ValueError:
            await message.answer(texts.RESTORE_BAD_LINK, reply_markup=kb.reply_hide())
            await _return_panel(message, services); return False
        except ServiceError as e:
            es = str(e)
            if es == "NEED_ENCRYPTION":
                msg = texts.NEED_ENCRYPTION_GUIDE
            elif es == "NOT_FULL_ACCESS":
                msg = ("Это не ссылка полного доступа. Пришли именно ссылку полного "
                       "доступа к серверу из приложения Amnezia.")
            elif es == "NOT_ADMIN_DEVICE":
                msg = ("Прикрепить полный доступ можно только к своему устройству или "
                       "к устройству без профиля. К устройству другого пользователя — "
                       "нельзя (иначе доступ к серверу окажется у него).")
            elif es.startswith("EXISTS:"):
                await state.set_state(RestoreDevice.link)
                await state.update_data(device_id=dev_id, mode="fa_transfer")
                await ask_tracked(message, services, texts.fa_transfer_warning(es.split(":", 1)[1]),
                                  reply_markup=kb.reply_hide())
                await ask_tracked(message, services,
                                  "Отправь ссылку ещё раз, чтобы подтвердить перенос, "
                                  "или нажми Отмена.", reply_markup=kb.reply_cancel())
                return True                       # диалог продолжается
            else:
                msg = es
            await message.answer(msg, reply_markup=kb.reply_hide())
            await _return_panel(message, services); return False
        await _delete_user_link(message)         # секрет из чата убираем
        await message.answer(texts.RESTORE_FULL_ACCESS_SAVED, reply_markup=kb.reply_hide())
        await _return_panel(message, services); return False

    if mode in ("fa", "fa_transfer"):
        await _attach(transfer=(mode == "fa_transfer"))
        return

    # клиентская реставрация; ФА-ссылку прозрачно уводим в ФА-путь
    try:
        await call(services.restore_app_device, dev_id, link)
    except ValueError:
        await message.answer(texts.RESTORE_BAD_LINK, reply_markup=kb.reply_hide())
        await _return_panel(message, services); return
    except ServiceError as e:
        es = str(e)
        if es == "IS_FULL_ACCESS":
            await _attach(transfer=False)        # единый вход: ФА-ссылка → ФА-путь
            return
        msg = texts.RESTORE_WRONG_DEVICE if es == "WRONG_DEVICE" else es
        await message.answer(msg, reply_markup=kb.reply_hide())
        await _return_panel(message, services); return
    await _delete_user_link(message)             # клиентская vpn:// несёт приватный ключ — убираем из чата
    _rdev = await call(services.db.get_device, dev_id)
    _rcl = await call(services.db.get_client, _rdev.client_id) if _rdev else None
    if _rdev and _rcl:
        _rtxt = (f"✅ Устройство «{_rdev.name}» (профиль «{_rcl.name}»): управление "
                 "восстановлено, ключ подключения прописан.")
    else:
        _rtxt = "✅ Устройство восстановлено — ключ подключения прописан."
    await message.answer(_rtxt, reply_markup=kb.reply_hide())
    await _return_panel(message, services)


# ─────────────────────────────────────────────────────────────────────────────
# Сервер: статус / бэкап / перезапуск
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "refresh"))
async def refresh_status(cb: CallbackQuery, services):
    """Внеплановое обновление статуса/метрик по кнопке (только админ — весь
    роутер под RoleFilter). Дёргает контейнер и /proc разово, пишет в state,
    затем перерисовывает панель из свежего state."""
    await cb.answer("Обновляю…")
    await call(services.refresh_status_now)
    await edit_nav(cb, services, await _panel_text(services), await _main_menu_markup(services))


@router.callback_query(AdminLinkGate.filter(
    (F.confirm == True) & (F.method.in_({"link", "qr", "file"}))))  # noqa: E712
async def admin_link_deliver(cb: CallbackQuery, callback_data: AdminLinkGate, services):
    """Подтверждено «Я понимаю, отдай» → отдаём СОХРАНЁННУЮ ссылку полного
    доступа (как есть) выбранным способом. Ключей не генерим — приложение
    Amnezia развернёт подключение само."""
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None or not getattr(dev, "is_admin", False):
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    try:
        link = await call(services.reveal_full_access_link, dev.id)
    except ServiceError as e:
        msg = ("Шифрование резервных копий выключено — не могу безопасно отдать ссылку."
               if str(e) == "NEED_ENCRYPTION" else str(e))
        await cb.answer(msg, show_alert=True)
        return
    method = callback_data.method
    if method == "link":
        await send_link(cb.message, link, services)
    elif method == "qr":
        await send_qr(cb.message, link, services)
    else:  # file
        await send_conf(cb.message, dev.name, link, services)
    await content_finisher(cb.message, services, texts.finish_link(dev.name), "admin")
    await cb.answer()


# ── Обновления бота (self-update) ────────────────────────────────────────────

@router.callback_query(UpdateCB.filter(F.action == "install"))
async def update_install(cb: CallbackQuery, services):
    """«Обновить» (из уведомления или меню обновления). Целевой поток:
    стереть цепочку до этого шага ВКЛЮЧИТЕЛЬНО (контент-сообщения + само
    сообщение с кнопкой), оставить единственное «дождись завершения» (без
    кнопок), запомнить его для удаления после рестарта и запустить апдейтер.
    Итог («успешно обновлен…» + changelog + «В меню») пришлёт новый процесс."""
    nxt = await call(services.update_next)
    if nxt is None:
        await cb.answer("Обновлять не на что — версия актуальна.", show_alert=True)
        return
    await cb.answer("Запускаю обновление…")
    chat_id = cb.message.chat.id
    await cleanup_content(cb.bot, services, chat_id)
    try:
        await cb.message.delete()                     # сам шаг — тоже в утиль
    except Exception:                                 # noqa: BLE001
        pass
    wait = await cb.bot.send_message(chat_id, texts.update_wait(nxt.tag))
    await call(services.set_update_wait, chat_id, wait.message_id)
    try:
        await call(services.apply_update, nxt)
        # успех: апдейтер вот-вот остановит сервис; «дождись» удалит и итог
        # пришлёт уже новый процесс (confirm_applied_update на старте).
    except Exception as e:                            # noqa: BLE001
        # не взлетело ещё ДО апдейтера (сеть/sha256/запись файла) — прибраться:
        # wait-сообщение, wait-ссылка и pending-флаг (иначе следующий рестарт
        # принесёт ложный «не применилось»)
        await call(services.pop_update_wait)
        await call(services.db.set_state, "update_pending", "")
        try:
            await wait.delete()
        except Exception:                             # noqa: BLE001
            pass
        await cb.bot.send_message(chat_id, texts.update_failed(str(e)),
                                  reply_markup=kb.update_done_menu())


@router.callback_query(UpdateCB.filter(F.action == "menu"))
async def update_menu(cb: CallbackQuery, services, state: FSMContext):
    """«В меню» на итоговом сообщении self-update: текст остаётся в истории,
    снимаем только клавиатуру; меню — новым сообщением."""
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                 # noqa: BLE001
        pass
    await show_main_menu(cb.message, services, "admin")
    await cb.answer()


@router.callback_query(UpdateCB.filter(F.action == "mute"))
async def update_mute(cb: CallbackQuery, services):
    """«Не уведомлять об обновлениях» — глушит автоуведомления и стартовую
    проверку. Само уведомление убираем; ручная кнопка остаётся живой."""
    await call(services.mute_updates)
    await cb.answer("Уведомления об обновлениях выключены.")
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Личный VPN админа: он тоже пользователь. Работаем с его клиентской записью
# (admin_client), переиспользуя те же тексты/клавиатуры/хелперы, что и клиент.
# Роль остаётся admin, поэтому это отдельные callback'и (AdminSelfCB).
# ─────────────────────────────────────────────────────────────────────────────

async def _self(services):
    """Клиентская запись админа (создаётся при старте; на всякий — гарантируем)."""
    ac = await call(services.admin_client)
    if ac is None:
        await call(services.ensure_admin_client)
        ac = await call(services.admin_client)
    return ac


@router.callback_query(AdminSelfCB.filter(F.action == "devices"))
async def self_devices(cb: CallbackQuery, services):
    ac = await _self(services)
    devices = await call(services.db.list_devices, ac.id)
    slots = await call(services.device_slots, ac.id)
    header = "<b>\U0001F4F1Мои устройства</b>\n\n" + texts.device_slots_line(*slots)
    await edit(cb, header, kb.client_devices(devices))
    await cb.answer()


@router.callback_query(AdminSelfCB.filter(F.action == "gen_link"))
async def self_gen_link(cb: CallbackQuery, services):
    ac = await _self(services)
    devices = await call(services.db.list_devices, ac.id)
    if not devices:
        await cb.answer("Сначала добавь устройство", show_alert=True)
        return
    await edit(cb, "Для какого устройства нужна ссылка?", kb.pick_device(devices, "gen_link"))
    await cb.answer()


@router.callback_query(AdminSelfCB.filter(F.action == "gen_qr"))
async def self_gen_qr(cb: CallbackQuery, services):
    ac = await _self(services)
    devices = await call(services.db.list_devices, ac.id)
    if not devices:
        await cb.answer("Сначала добавь устройство", show_alert=True)
        return
    await edit(cb, "Для какого устройства нужен QR-код?", kb.pick_device(devices, "gen_qr"))
    await cb.answer()


@router.callback_query(AdminSelfCB.filter(F.action == "gen_file"))
async def self_gen_file(cb: CallbackQuery, services):
    ac = await _self(services)
    devices = await call(services.db.list_devices, ac.id)
    if not devices:
        await cb.answer("Сначала добавь устройство", show_alert=True)
        return
    await edit(cb, "Для какого устройства нужен файл?", kb.pick_device(devices, "gen_file"))
    await cb.answer()


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


@router.callback_query(AdminSelfCB.filter(F.action == "add"))
async def self_add_start(cb: CallbackQuery, services, state: FSMContext):
    ac = await _self(services)
    used, limit = await call(services.device_slots, ac.id)
    if limit != 0 and used >= limit:
        await cb.answer("Лимит устройств исчерпан", show_alert=True)
        return
    await state.set_state(AdminSelfAddDevice.name)
    await ask_tracked(cb.message, services, "Введи имя нового устройства:", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(AdminSelfAddDevice.name)
async def self_add_name(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, "Имя не может быть пустым. Введи ещё раз:")
        return
    await state.update_data(dev_name=name)
    await state.set_state(AdminSelfAddDevice.traffic)
    await ask_tracked(message, services, texts.traffic_limit_device_ask(0), reply_markup=kb.reply_cancel())


@router.message(AdminSelfAddDevice.traffic)
async def self_add_traffic(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.TRAFFIC_LIMIT_BAD)
        return
    data = await state.get_data()
    name = data.get("dev_name")
    tlimit = int(raw) * BYTES_PER_GB
    await state.clear()
    ac = await _self(services)
    try:
        created = await call(services.add_device, ac.id, name, tlimit)
    except (LimitReached, ServiceError) as e:
        await message.answer(f"Не удалось создать устройство: {e}", reply_markup=kb.reply_hide())
        await _return_panel(message, services)
        return
    await message.answer(f"\u2705 Устройство \u00ab{texts._e(name)}\u00bb создано.",
                         reply_markup=kb.reply_hide())
    dev = await call(services.db.get_device, created.device_id)
    back = Menu(action="main").pack()
    await send_menu(message, services, texts.CONNECT_METHOD_ASK,
                    kb.connect_method_choice(dev.id, back))


@router.callback_query(Menu.filter(F.action == "devices"))
async def admin_menu_devices(cb: CallbackQuery, services):
    """«Назад» из карточки устройства (Menu devices). Для админа список
    устройств = его собственные (чужие он смотрит через карточку клиента)."""
    await self_devices(cb, services)


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


@router.callback_query(DeviceCB.filter(F.action == "add"))
async def admin_dev_add_alias(cb: CallbackQuery, services, state: FSMContext):
    """Алиас: протухшая кнопка DeviceCB(add) из старых сообщений админа →
    ведём в его личное добавление (актуальные клавиатуры шлют AdminSelfCB)."""
    await self_add_start(cb, services, state)


# ─────────────────────────────────────────────────────────────────────────────
# Ручные блокировки (админ): устройство и клиент
# ─────────────────────────────────────────────────────────────────────────────

from awgbot.core.blocks import DeviceBlock as DeviceBlock, ClientBlock as ClientBlock

_KIND_TO_DEV = {"silent": DeviceBlock.ADMIN_SILENT, "notified": DeviceBlock.ADMIN_NOTIFIED,
                "user": DeviceBlock.USER}
_KIND_TO_CLI = {"silent": ClientBlock.ADMIN_SILENT, "notified": ClientBlock.ADMIN_NOTIFIED,
                "user": ClientBlock.USER}


async def _rerender_after_block(cb, services, target: str, ref: int):
    """Перерисовать карточку устройства/клиента после изменения блокировки."""
    if target == "cli":
        await _show_client_card(cb, services, ref)
    else:
        dev = await call(services.db.get_device, ref)
        if dev:
            text = texts.device_card_text(dev, for_admin=True)
            marker = texts.friend_marker(dev)
            if marker:
                text += f"\n\n{marker}"
            back_target, reassign_label = await _device_back_target_and_label(services, dev)
            await edit(cb, text, kb.device_actions(dev, is_admin=True, back_target=back_target,
                                                    reassign_label=reassign_label))


@router.callback_query(BlockCB.filter(F.action == "menu_block"))
async def admin_block_menu(cb: CallbackQuery, callback_data: BlockCB):
    """Админ жмёт «Заблокировать». Для КЛИЕНТА сперва спрашиваем про приостановку
    подписки; для устройства — сразу выбор уведомления (пауза только для клиента)."""
    if callback_data.target == "cli":
        await edit(cb, "Приостановить подписку на время блокировки?",
                   kb.block_pause_choice(callback_data.ref))
    else:
        await edit(cb, "Как заблокировать устройство?",
                   kb.block_notify_choice("dev", callback_data.ref))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "pause_no"))
async def admin_block_pause_no(cb: CallbackQuery, callback_data: BlockCB):
    """Блок клиента без приостановки → выбор уведомления (pause_days=-1)."""
    await edit(cb, "Как заблокировать профиль?",
               kb.block_notify_choice("cli", callback_data.ref, pause_days=-1))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "pause_yes"))
async def admin_block_pause_yes(cb: CallbackQuery, callback_data: BlockCB, state: FSMContext):
    """Блок клиента с приостановкой → ввод длительности (0 = бессрочно)."""
    await state.set_state(BlockPauseDays.days)
    await state.update_data(block_client=callback_data.ref)
    await cb.message.answer(
        "На сколько дней приостановить подписку? Введи число (0 — бессрочно, "
        "до снятия блокировки).", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(BlockPauseDays.days)
async def admin_block_pause_days(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await message.answer("Введи целое число дней (0 — бессрочно):")
        return
    data = await state.get_data()
    client_id = data.get("block_client")
    await state.clear()
    days = int(raw)
    _acc = await message.answer("Принято.", reply_markup=kb.reply_hide())
    await call(services.db.add_content_msg_id, _acc.chat.id, _acc.message_id)
    await message.answer(
        f"Приостановка: {'бессрочно' if days == 0 else f'{days} дн.'}. "
        "Как заблокировать профиль?",
        reply_markup=kb.block_notify_choice("cli", client_id, pause_days=days))


@router.callback_query(BlockCB.filter(F.action == "menu_unblock"))
async def admin_unblock_menu(cb: CallbackQuery, callback_data: BlockCB, services):
    """Админ жмёт «Разблокировать». Если активна ровно одна ручная причина —
    снимаем сразу, без диалога (он избыточен — выбирать не из чего). Если
    несколько — спрашиваем, какую именно."""
    target, ref = callback_data.target, callback_data.ref
    if target == "cli":
        obj = await call(services.db.get_client, ref)
        kind_map = _KIND_TO_CLI
    else:
        obj = await call(services.db.get_device, ref)
        kind_map = _KIND_TO_DEV
    if obj is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    mask = int(obj.block_reason)
    active_kinds = [k for k, bit in kind_map.items() if mask & int(bit)]
    if len(active_kinds) <= 1:
        kind = active_kinds[0] if active_kinds else "all"
        await _do_unblock(cb, services, target, ref, kind)
        await cb.answer("Разблокировано")
        return
    await edit(cb, "Какую блокировку снять?",
               kb.block_unblock_reasons(target, ref, mask))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "block"))
async def admin_block_do(cb: CallbackQuery, callback_data: BlockCB, services):
    notify = callback_data.kind == "notified"
    pd = callback_data.days
    pause_days = None if pd < 0 else pd         # -1 = без приостановки
    if callback_data.target == "cli":
        bit = _KIND_TO_CLI["notified" if notify else "silent"]
        notes = await call(services.block_client_manual, callback_data.ref, bit,
                           notify, pause_days)
    else:
        bit = _KIND_TO_DEV["notified" if notify else "silent"]
        notes = await call(services.block_device_manual, callback_data.ref, bit, notify)
    await send_notifications(cb.bot, notes)
    await _rerender_after_block(cb, services, callback_data.target, callback_data.ref)
    if callback_data.target == "cli":
        _o = await call(services.db.get_client, callback_data.ref)
        _what = f"🛑 Профиль «{_o.name}» заблокирован" if _o else "🛑 Профиль заблокирован"
    else:
        _o = await call(services.db.get_device, callback_data.ref)
        _what = f"🛑 Устройство «{_o.name}» заблокировано" if _o else "🛑 Устройство заблокировано"
    await cb.answer(_what + ("" if notify else " (тихо)"))


async def _do_unblock(cb, services, target: str, ref: int, kind: str):
    """Снимает блокировку (kind или 'all') + уведомления + перерисовка карточки.
    Общий путь и для диалога выбора, и для авто-снятия единственной причины."""
    if target == "cli":
        obj = await call(services.db.get_client, ref)
        mask = int(obj.block_reason) if obj else 0
        kinds = (["silent", "notified", "user"] if kind == "all" else [kind])
        notes = []
        for k in kinds:
            bit = _KIND_TO_CLI.get(k)
            if bit is None or not (mask & int(bit)):
                continue
            notes += await call(services.unblock_client_manual, ref, bit, k != "silent")
    else:
        obj = await call(services.db.get_device, ref)
        mask = int(obj.block_reason) if obj else 0
        kinds = (["silent", "notified", "user"] if kind == "all" else [kind])
        notes = []
        for k in kinds:
            bit = _KIND_TO_DEV.get(k)
            if bit is None or not (mask & int(bit)):
                continue
            notes += await call(services.unblock_device_manual, ref, bit, k != "silent")
    await send_notifications(cb.bot, notes)
    await _rerender_after_block(cb, services, target, ref)


@router.callback_query(BlockCB.filter(F.action == "unblock"))
async def admin_unblock_do(cb: CallbackQuery, callback_data: BlockCB, services):
    await _do_unblock(cb, services, callback_data.target, callback_data.ref, callback_data.kind)
    await cb.answer("Разблокировано")


@router.callback_query(BlockCB.filter(F.action == "cancel"))
async def admin_block_cancel(cb: CallbackQuery, callback_data: BlockCB, services):
    await _rerender_after_block(cb, services, callback_data.target, callback_data.ref)
    await cb.answer()


__all__ = ["router"]


# ── Броадкаст объявлений всем клиентам и друзьям ─────────────────────────────
_BROADCAST_COOLDOWN_SEC = 30          # анти-дабл-тап: не слать одно и то же чаще
_last_broadcast_at = 0.0


@router.callback_query(BroadcastCB.filter(F.action == "start"))
async def broadcast_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.text)
    await edit(cb, texts.BROADCAST_PROMPT, kb.broadcast_cancel())
    await cb.answer()


@router.message(Broadcast.text)
async def broadcast_receive(message: Message, state: FSMContext, services):
    text = (message.text or "").strip()
    if not text:
        await message.answer(texts.BROADCAST_EMPTY)
        return
    n = len(services.db.broadcast_recipients(config.ADMIN_ID))
    if n == 0:
        await state.clear()
        await message.answer("Некому отправлять — нет активных получателей.")
        return
    # текст держим в FSM-data до подтверждения; отправляем HTML как есть.
    # Битую разметку ловим здесь: если превью (обёрнутое в HTML) не отправилось —
    # тот же текст провалил бы и рассылку. Просим поправить, состояние держим.
    await state.update_data(text=text)
    try:
        await message.answer(texts.broadcast_preview(text, n), reply_markup=kb.broadcast_confirm())
    except TelegramBadRequest:
        await message.answer(
            "⚠️ Разметка бракованная (незакрытый тег?). Проверь HTML "
            "(&lt;b&gt;…&lt;/b&gt;, ссылки) и пришли текст заново.")


@router.callback_query(BroadcastCB.filter(F.action == "send"))
async def broadcast_send(cb: CallbackQuery, state: FSMContext, services):
    global _last_broadcast_at
    data = await state.get_data()
    text = data.get("text")
    await state.clear()
    if not text:
        await cb.answer("Нечего отправлять.", show_alert=True)
        return
    now = time.monotonic()
    if now - _last_broadcast_at < _BROADCAST_COOLDOWN_SEC:
        await cb.answer("Только что уже отправляли — подожди немного.", show_alert=True)
        return
    _last_broadcast_at = now                     # метку ставим ДО await'ов —
    await cb.answer("Рассылаю…")                 # второе нажатие уже отсечётся
    await edit(cb, "📢 Рассылаю объявление…", None)   # и кнопки сняты (markup=None)
    tg_ids = await call(services.db.broadcast_recipients, config.ADMIN_ID)
    if not tg_ids:
        await edit(cb, "Некому отправлять — нет активных получателей.",
                   await _main_menu_markup(services))
        return
    ok, failed = await broadcast(cb.message.bot, tg_ids, text)
    await edit(cb, texts.broadcast_report(ok, failed), await _main_menu_markup(services))
