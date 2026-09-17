"""
handlers/admin/clients.py — профили (клиенты) глазами администратора.

Список, карточка, создание с инвайтом, имя / лимиты / период, продление с
остатком, перевыпуск инвайта, удаление, вывод из приостановки.
"""

from __future__ import annotations

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.util import timeutil
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import ClientCB, ConfirmCB, DeviceCB, Menu, PeriodCB
from awgbot.bot.handlers.common import (call, edit, edit_nav, ask_tracked, remove_device_and_notify,
                                        send_menu, content_finisher)
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.domain.services import BYTES_PER_GB, ServiceError
from awgbot.bot.states import CreateClient, EditLimit, EditName, EditPeriod, EditTrafficLimit
from awgbot.bot.handlers.admin.panel import (_expiring_screen, _extend_picker, _main_menu_markup,
                                             _panel_parts, _return_panel)

router = Router(name="admin.clients")


# ─────────────────────────────────────────────────────────────────────────────
# Клиенты: список / карточка
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "clients"))
async def clients_list(cb: CallbackQuery, services):
    # Профиль админа СКРЫТ: весь его функционал всегда есть на главной (свои
    # устройства, конфиги, РФ-доступ), а карточка была урезана до тех же кнопок.
    # Строка в списке дублировала главную и путала, кто тут кем управляет.
    await cb.answer()
    clients = await call(services.db.list_clients, exclude_tg=config.ADMIN_ID)
    if not clients:
        await edit_nav(cb, services, "Профилей пока нет.", await _main_menu_markup(services))
    else:
        online = await call(services.online_client_ids)
        await edit(cb, "👥 Профили:", kb.admin_clients(clients, online))


async def _client_card_parts(services, client_id: int):
    """(текст, клавиатура) карточки профиля или None — профиля нет."""
    d = await call(services.client_card_data, client_id)     # один хоп вместо восьми
    if d is None:
        return None
    client, devices = d["client"], d["devices"]
    text = texts.client_card(client, devices, d["traffic"], d["online"], for_admin=True)
    # Прогресс переезда — последней строкой и ТОЛЬКО админу: клиенту знать про
    # внутреннюю кухню незачем, а карточку он видит в своём варианте.
    if d["progress"] is not None:
        line = texts.migration_profile_line(*d["progress"])
        if line:
            text += "\n\n" + line
    return text, kb.admin_client_actions(
        client, has_devices=bool(devices), is_admin_owner=client.tg_id == config.ADMIN_ID,
        routing_visible=d["rt_visible"], routing_on=d["rt_on"])


async def _show_client_card(cb: CallbackQuery, services, client_id: int):
    parts = await _client_card_parts(services, client_id)
    if parts is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    await edit(cb, *parts)


@router.callback_query(ClientCB.filter(F.action == "open"))
async def client_open(cb: CallbackQuery, callback_data: ClientCB, services):
    await cb.answer()
    await _show_client_card(cb, services, callback_data.client_id)


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


async def _apply_limit(message, services, client, new_limit: int, *, via_edit=None):
    """Применить лимит и отчитаться. Итог — на месте вопроса (via_edit — колбэк
    подтверждения) либо новым сообщением; панель следом. Раньше подтверждение
    переписывалось в «Готово.» с кнопками меню уже ПОСЛЕ присланной панели, и
    та оставалась в чате мёртвой — живое меню висело над ней."""
    old_limit = client.device_limit
    await call(services.db.update_client_fields, client.id, device_limit=new_limit)
    old_s = "без ограничения" if not old_limit else str(old_limit)
    new_s = "без ограничения" if not new_limit else str(new_limit)
    done = f"✅ Лимит устройств профиля «{texts._e(client.name)}» изменён: {old_s} → {new_s}."
    if via_edit is not None:
        await edit(via_edit, done, None)
    else:
        await message.answer(done, reply_markup=kb.reply_hide())
    if client.tg_id and old_limit != new_limit:
        await notify_one(message.bot, client.tg_id,
                         texts.limit_changed_notice(old_limit, new_limit))
    await _return_panel(message, services,
                        keep_id=via_edit.message.message_id if via_edit is not None else None)


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
    await cb.answer()
    await _apply_limit(cb.message, services, client, new_limit, via_edit=cb)


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
    _vname = target.name if target else "?"
    devices = await call(services.db.list_devices, callback_data.ref)
    failed: list[str] = []
    for d in devices:
        try:
            await remove_device_and_notify(cb.bot, services, d.id)
        except ServiceError:
            failed.append(d.name)
    # Пир не снялся с сервера — профиль НЕ удаляем. Удалить запись, оставив пир
    # живым, значит: доступ у человека продолжает работать, а запись, по которой
    # его можно найти, исчезла. Из двух неполных состояний это строго худшее, и
    # молчать о нём нельзя — соседний поток (удаление одного устройства) на том
    # же отказе останавливается и показывает причину.
    if failed:
        await edit(cb, texts.CLIENT_DELETE_PARTIAL.format(
            name=texts._e(_vname),
            devices=texts._e(", ".join(failed))),
            kb.admin_client_back(callback_data.ref))
        await cb.answer("Сервер не ответил — ничего не удалено", show_alert=True)
        return
    await call(services.db.delete_client, callback_data.ref)
    await edit_nav(cb, services,
                   f"🗑 Профиль «{_vname}» удалён (устройств удалено: {len(devices)}).",
                   await _main_menu_markup(services))
    await cb.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Продление (период → при остатке спрашиваем сохранение)
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "extend"))
async def extend_start(cb: CallbackQuery, callback_data: ClientCB, services):
    screen = await _extend_picker(services, callback_data.client_id)
    if screen is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    await edit(cb, *screen)
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
        return_to = (await state.get_data()).get("return_to")
        await state.clear()
        await _do_extend(cb, services, client_id, callback_data.kind, keep=False,
                         return_to=return_to)
    await cb.answer()


@router.callback_query(ConfirmCB.filter(F.action == "keep"))
async def extend_keep_answer(cb: CallbackQuery, callback_data: ConfirmCB, services, state: FSMContext):
    data = await state.get_data()
    kind = data.get("extend_kind")
    client_id = data.get("extend_client") or callback_data.ref
    return_to = data.get("return_to")
    await state.clear()
    if not kind:
        await cb.answer("Диалог прерван, начни заново", show_alert=True)
        return
    await _do_extend(cb, services, client_id, kind, keep=callback_data.yes, return_to=return_to)
    await cb.answer()


_PERIOD_ACC = {"day": "день", "week": "неделю", "month": "месяц", "year": "год"}


async def _do_extend(cb, services, client_id, kind, keep: bool, return_to: str | None = None):
    """Итог продления — ИНФОСООБЩЕНИЕМ на месте диалога (остаётся в чате), меню
    следом со своим обычным текстом: раньше текст итога садился в само меню и
    дублировал уведомление. return_to="expiring" — назад в список истекающих,
    пока он не пуст; опустел — в меню."""
    try:
        result = await call(services.extend_period, client_id, kind, keep)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await send_notifications(cb.bot, result.notifications)
    fresh = await call(services.db.get_client, client_id)
    name = fresh.name if fresh else "?"
    if result.new_end is None:
        done = (f"✅ Период подписки профиля {name} успешно изменён.\n"
                "<b>Подписка теперь бессрочная.</b>")
    else:
        done = (f"✅ Подписка профиля {name} продлена на 1 {_PERIOD_ACC.get(kind, kind)}, "
                f"до {timeutil.fmt_dt(result.new_end)}")
        pause_line = texts.pause_credit_admin(result.pause)
        if pause_line:
            done += f"\n{pause_line}"
    await edit(cb, done, None)
    keep = cb.message.message_id
    if return_to == "expiring" and await call(services.expiring_subscriptions):
        await send_menu(cb.message, services, *await _expiring_screen(services), keep_id=keep)
        return
    await send_menu(cb.message, services, *await _panel_parts(services), keep_id=keep)


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
        f"текущую: {cur_txt})\nФормат ввода: DD.MM.YYYY HH:MM:SS (время можно опустить — будет 00:00:00)",
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
                                 "«как есть». Введи дату (DD.MM.YYYY, время можно добавить):",
                                 reply_markup=kb.reply_cancel())
            return
    else:
        try:
            new_start = timeutil.parse_dt_sec(raw)
        except ValueError:
            await ask_tracked(message, services, "Не разобрал дату. Формат: DD.MM.YYYY HH:MM:SS, время можно опустить. "
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
        f"Формат ввода: DD.MM.YYYY HH:MM:SS (время можно опустить — будет 00:00:00)",
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
            await ask_tracked(message, services, "Не разобрал дату. Формат: DD.MM.YYYY HH:MM:SS, время можно опустить. "
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
        f"✅ Период подписки профиля {client.name} успешно изменён.\n"
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
    await cb.answer("Возобновлено")
    # итог — на месте карточки и остаётся в чате, карточка — следом (как у
    # продления и переезда: живое меню всегда последним сообщением)
    await edit(cb, f"▶️ Профиль «{texts._e(client.name)}» выведен из приостановки.\n"
                   f"Списано дней: {actual}. Новый срок: {end_txt}.", None)
    parts = await _client_card_parts(services, client.id)
    if parts is not None:
        await send_menu(cb.message, services, *parts, keep_id=cb.message.message_id)
