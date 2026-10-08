"""
handlers/admin/clients.py — профили глазами администратора.

Список, карточка, подменю «✏️ Изменить», создание пресетами с приглашением,
имя / лимиты / период с итогом первой строкой, продление с тумблером остатка,
удаление, новое приглашение, снятие паузы. Ввод текста — на месте экрана
(ask_here), возврат — в экран-контекст (back_to_context).
"""

from __future__ import annotations

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.util import timeutil
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import ClientCB, Menu, PeriodCB, PresetCB
from awgbot.bot.handlers.common import (call, edit, ask_here, ask_tracked, back_to_context,
                                        cleanup_content, send_menu, drop_message)
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.domain.services import BYTES_PER_GB, ServiceError
from awgbot.bot.states import CreateClient, EditLimit, EditName, EditPeriod, EditTrafficLimit
from awgbot.bot.handlers.admin.panel import (expiring_screen, _panel_parts, _return_panel, _bot)

router = Router(name="admin.clients")


# ─────────────────────────────────────────────────────────────────────────────
# Список / карточка / «✏️ Изменить»
# ─────────────────────────────────────────────────────────────────────────────

async def clients_screen(services, chat_id: int = 0):
    clients = await call(services.db.list_clients, exclude_tg=config.ADMIN_ID)
    online = await call(services.online_client_ids)
    from awgbot.bot import paging
    n_online = sum(1 for c in clients if c.id in online)
    return (texts.profiles_header(len(clients), n_online),
            kb.admin_clients(clients, online, page=paging.page_of(chat_id, "clients")))


@router.callback_query(Menu.filter(F.action == "clients"))
async def clients_list(cb: CallbackQuery, services, state: FSMContext):
    # Профиль админа скрыт: его устройства и РФ-доступ — на главной.
    await state.clear()
    await cb.answer()
    await edit(cb, *await clients_screen(services, cb.message.chat.id))


async def client_card_parts(services, client_id: int):
    """(текст, клавиатура) карточки профиля или None — профиля нет."""
    d = await call(services.client_card_data, client_id)
    if d is None or d["client"].tg_id == config.ADMIN_ID:   # у профиля админа карточки нет
        return None
    client = d["client"]
    if d.get("rt_visible"):
        d["rt_counts"] = await call(services.routing_device_counts, client_id)
    return (texts.admin_client_card(d, _bot(services)),
            kb.admin_client_actions(client, d["devices"], routing_visible=d["rt_visible"]))


async def client_edit_parts(services, client_id: int):
    client = await call(services.db.get_client, client_id)
    if client is None:
        return None
    return texts.client_edit_text(client), kb.client_edit_kb(client_id)


async def _show_client_card(cb: CallbackQuery, services, client_id: int, *, answer: bool = True):
    parts = await client_card_parts(services, client_id)
    if parts is None:
        client = await call(services.db.get_client, client_id)
        if client is not None and client.tg_id == config.ADMIN_ID:
            from awgbot.bot.handlers.admin import panel      # профиль админа — главная
            await edit(cb, *await panel._panel_parts(services))
            if answer:
                await cb.answer()
            return
        await cb.answer("Профиль не найден", show_alert=True)   # единственный ответ: иначе alert теряется
        return
    await edit(cb, *parts)
    if answer:                                 # answer=False — вызывающий ответит своей всплывашкой
        await cb.answer()


@router.callback_query(ClientCB.filter(F.action == "open"))
async def client_open(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    await state.clear()
    await _show_client_card(cb, services, callback_data.client_id)


@router.callback_query(ClientCB.filter(F.action == "edit"))
async def client_edit(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    await state.clear()
    parts = await client_edit_parts(services, callback_data.client_id)
    if parts is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    await edit(cb, *parts)
    await cb.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Новый профиль: имя → устройства → трафик → срок (пресетами)
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "add_client"))
async def add_client_start(cb: CallbackQuery, services, state: FSMContext):
    await state.set_state(CreateClient.name)
    await ask_here(cb, services, state, texts.NEW_PROFILE_NAME, "main")
    await cb.answer()


@router.message(CreateClient.name)
async def add_client_name(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    await state.update_data(name=name)
    await state.set_state(CreateClient.limit)
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, texts.new_profile_devs(name), kb.new_profile_devs_kb())


@router.callback_query(PresetCB.filter(F.kind == "new_devs"), CreateClient.limit)
async def add_client_devs_preset(cb: CallbackQuery, callback_data: PresetCB, services, state: FSMContext):
    data = await state.get_data()
    name = data.get("name") or ""
    if callback_data.val < 0:
        await ask_here(cb, services, state, texts.OTHER_NUMBER_PROMPT, "main")
        await cb.answer()
        return
    await _devs_chosen(cb.message, services, state, name, int(callback_data.val), via_cb=cb)


async def _devs_chosen(message: Message, services, state: FSMContext, name: str, limit: int,
                       via_cb: CallbackQuery | None = None) -> None:
    await state.update_data(limit=limit)
    await state.set_state(CreateClient.traffic)
    text, markup = texts.new_profile_traffic(name, limit), kb.new_profile_traffic_kb()
    if via_cb is not None:
        await edit(via_cb, text, markup)
        await via_cb.answer()
        return
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, text, markup)


@router.message(CreateClient.limit)
async def add_client_limit(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD_LIMIT)
        return
    name = (await state.get_data()).get("name") or ""
    await _devs_chosen(message, services, state, name, int(raw))


@router.callback_query(PresetCB.filter(F.kind == "new_traffic"), CreateClient.traffic)
async def add_client_traffic_preset(cb: CallbackQuery, callback_data: PresetCB, services, state: FSMContext):
    data = await state.get_data()
    if callback_data.val < 0:
        await ask_here(cb, services, state, texts.OTHER_NUMBER_PROMPT, "main")
        await cb.answer()
        return
    await _traffic_chosen(cb.message, services, state, data, int(callback_data.val), via_cb=cb)


async def _traffic_chosen(message: Message, services, state: FSMContext, data: dict, gb_value: int,
                          via_cb: CallbackQuery | None = None) -> None:
    await state.update_data(traffic_gb=gb_value)
    name, limit = data.get("name") or "", int(data.get("limit") or 0)
    text = texts.new_profile_period(name, limit, gb_value)
    markup = kb.period_kb("create")
    if via_cb is not None:
        await edit(via_cb, text, markup)
        await via_cb.answer()
        return
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, text, markup)


@router.message(CreateClient.traffic)
async def add_client_traffic(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD_LIMIT)
        return
    await _traffic_chosen(message, services, state, await state.get_data(), int(raw))


@router.callback_query(PeriodCB.filter(F.ctx == "create"))
async def add_client_period(cb: CallbackQuery, callback_data: PeriodCB, services, state: FSMContext):
    data = await state.get_data()
    name = data.get("name")
    limit = data.get("limit")
    traffic_gb = data.get("traffic_gb")
    await state.clear()
    if not name or limit is None or traffic_gb is None:  # limit/traffic=0 валидны
        await cb.answer("Диалог устарел — открой создание профиля заново", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                              # noqa: BLE001
            pass
        await _return_panel(cb.message, services)
        return
    try:
        created = await call(services.create_client, name, int(limit), callback_data.kind,
                             int(traffic_gb) * BYTES_PER_GB)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    client = await call(services.db.get_client, created.client_id)
    await cb.answer()
    await drop_message(cb, services)
    await _invite_menu(cb.message, services, client, created.invite_code, new=True)


async def _invite_menu(message: Message, services, client, code: str, *, new: bool) -> None:
    """Приглашение одним сообщением-меню: текст для пересылки, черта, сводка
    профиля; кнопки отправки и копирования несут только текст приглашения,
    выходы — в карточку и на главную (сообщение при этом перерисовывается)."""
    bot = _bot(services) or (await message.bot.me()).username
    link = f"https://t.me/{bot}?start={code}"
    await send_menu(message, services, texts.invite_screen(link, client, new=new),
                    kb.invite_menu(texts.invite_plain(link), link, client.id))


# ─────────────────────────────────────────────────────────────────────────────
# Имя / лимиты / период — с возвратом в «✏️ Изменить»
# ─────────────────────────────────────────────────────────────────────────────

async def _client_or_alert(cb: CallbackQuery, services, client_id: int):
    client = await call(services.db.get_client, client_id)
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
    return client


@router.callback_query(ClientCB.filter(F.action == "edit_name"))
async def edit_name_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    is_admin = client.tg_id == config.ADMIN_ID
    await state.set_state(EditName.value)
    await ask_here(cb, services, state, texts.client_name_prompt(client),
                   "cl" if is_admin else "edit", client.id, client_id=client.id)
    await cb.answer()


@router.message(EditName.value)
async def edit_name_apply(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    data = await state.get_data()
    await state.clear()
    old = await call(services.db.get_client, data.get("client_id"))
    if old is None:
        await back_to_context(message, services, {}, "admin")
        return
    await call(services.db.update_client_fields, old.id, name=name)
    await back_to_context(message, services, data, "admin", note=texts.client_name_note(old.name, name))


@router.callback_query(ClientCB.filter(F.action == "edit_limit"))
async def edit_limit_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    await state.clear()
    used = await call(services.db.count_devices, client.id)
    await edit(cb, texts.devs_limit_prompt(client, used), kb.devs_limit_kb(client.id))
    await cb.answer()


async def _apply_devs_limit(cb_or_msg, services, client, new_limit: int, *, via_cb: CallbackQuery | None,
                            data: dict | None = None) -> None:
    """Применить лимит устройств: без подтверждения, итог первой строкой
    «✏️ Изменить», клиенту — уведомление."""
    old_limit = int(client.device_limit)
    used = await call(services.set_device_limit, client.id, new_limit)
    note = texts.devs_limit_note(old_limit, new_limit, used)
    if client.tg_id and old_limit != new_limit and client.tg_id != config.ADMIN_ID:
        await notify_one(cb_or_msg.bot, client.tg_id, texts.limit_changed_notice(old_limit, new_limit))
    if via_cb is not None:
        from awgbot.bot import screens
        text, markup = await client_edit_parts(services, client.id)
        await edit(via_cb, screens.with_note(text, note), markup)
        await via_cb.answer()
        return
    await back_to_context(cb_or_msg, services, data or {"ctx_kind": "edit", "ctx_ref": client.id},
                          "admin", note=note)


@router.callback_query(PresetCB.filter(F.kind == "cli_devs"))
async def edit_limit_preset(cb: CallbackQuery, callback_data: PresetCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.ref)
    if client is None:
        return
    if callback_data.val < 0:
        await state.set_state(EditLimit.value)
        await ask_here(cb, services, state, texts.OTHER_NUMBER_PROMPT, "edit", client.id,
                       client_id=client.id)
        await cb.answer()
        return
    await _apply_devs_limit(cb, services, client, int(callback_data.val), via_cb=cb)


@router.message(EditLimit.value)
async def edit_limit_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD_LIMIT)
        return
    data = await state.get_data()
    await state.clear()
    client = await call(services.db.get_client, data.get("client_id"))
    if client is None:
        await back_to_context(message, services, {}, "admin")
        return
    await _apply_devs_limit(message, services, client, int(raw), via_cb=None, data=data)


@router.callback_query(ClientCB.filter(F.action == "edit_traffic"))
async def edit_client_traffic_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    await state.clear()
    await edit(cb, texts.traffic_limit_prompt(client), kb.traffic_limit_kb(client.id))
    await cb.answer()


async def _apply_traffic_limit(cb_or_msg, services, client, gb_value: int, *, via_cb: CallbackQuery | None,
                               data: dict | None = None) -> None:
    old_b = int(client.traffic_limit)
    new_b = gb_value * BYTES_PER_GB
    await call(services.set_client_traffic_limit, client.id, new_b)
    note = texts.traffic_limit_note(old_b, new_b)
    if via_cb is not None:
        from awgbot.bot import screens
        text, markup = await client_edit_parts(services, client.id)
        await edit(via_cb, screens.with_note(text, note), markup)
        await via_cb.answer()
        return
    await back_to_context(cb_or_msg, services, data or {"ctx_kind": "edit", "ctx_ref": client.id},
                          "admin", note=note)


@router.callback_query(PresetCB.filter(F.kind == "cli_traffic"))
async def edit_traffic_preset(cb: CallbackQuery, callback_data: PresetCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.ref)
    if client is None:
        return
    if callback_data.val < 0:
        await state.set_state(EditTrafficLimit.value)
        await ask_here(cb, services, state, texts.OTHER_NUMBER_PROMPT, "edit", client.id,
                       target="client", client_id=client.id)
        await cb.answer()
        return
    await _apply_traffic_limit(cb, services, client, int(callback_data.val), via_cb=cb)


@router.message(EditTrafficLimit.value)
async def edit_traffic_apply(message: Message, services, state: FSMContext):
    """Ввод лимита профиля или устройства («✏️ Другое»)."""
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD_LIMIT)
        return
    data = await state.get_data()
    if data.get("target") == "device":
        from awgbot.bot.handlers.admin.devices import apply_device_limit_typed
        await apply_device_limit_typed(message, services, state, data, int(raw))
        return
    await state.clear()
    client = await call(services.db.get_client, data.get("client_id"))
    if client is None:
        await back_to_context(message, services, {}, "admin")
        return
    await _apply_traffic_limit(message, services, client, int(raw), via_cb=None, data=data)


# ── период вручную ───────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "edit_period"))
async def edit_period_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    await state.set_state(EditPeriod.start)
    await ask_here(cb, services, state, texts.period_start_prompt(client), "edit", client.id,
                   client_id=client.id)
    await cb.answer()


@router.message(EditPeriod.start)
async def edit_period_start_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    data = await state.get_data()
    client = await call(services.db.get_client, data.get("client_id"))
    if client is None:
        await state.clear()
        await back_to_context(message, services, {}, "admin")
        return
    if raw == "-":
        new_start = client.period_start and timeutil.parse_iso(client.period_start)
        if new_start is None:
            await ask_tracked(message, services, texts.PERIOD_NO_START)
            return
    else:
        try:
            new_start = timeutil.parse_dt_sec(raw)
        except ValueError:
            await ask_tracked(message, services, texts.PERIOD_BAD)
            return
    await state.update_data(new_start=timeutil.to_iso(new_start))
    await state.set_state(EditPeriod.end)
    await ask_tracked(message, services, texts.period_end_prompt(client))


@router.message(EditPeriod.end)
async def edit_period_end_apply(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    data = await state.get_data()
    client = await call(services.db.get_client, data.get("client_id"))
    if client is None:
        await state.clear()
        await back_to_context(message, services, {}, "admin")
        return
    if raw == "-":
        new_end = client.period_end and timeutil.parse_iso(client.period_end)
    elif raw == "0":
        new_end = None
    else:
        try:
            new_end = timeutil.parse_dt_sec(raw)
        except ValueError:
            await ask_tracked(message, services, texts.PERIOD_BAD)
            return
    await state.clear()
    saved_start = data.get("new_start")
    new_start = timeutil.parse_iso(saved_start) if saved_start else None
    if new_start is None:
        await back_to_context(message, services, data, "admin", note=texts.PERIOD_NO_START)
        return
    try:
        s, e, notes = await call(services.set_subscription_dates, client.id, new_start, new_end)
    except ServiceError as ex:
        await back_to_context(message, services, data, "admin", note=f"⚠️ {texts._e(str(ex))}")
        return
    await send_notifications(message.bot, notes)
    await back_to_context(message, services, data, "admin", note=texts.period_changed_note(s, e))


# ─────────────────────────────────────────────────────────────────────────────
# Новое приглашение / удаление
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "regen_invite"))
async def regen_invite(cb: CallbackQuery, callback_data: ClientCB, services):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    try:
        code = await call(services.regenerate_invite, client.id)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer("Новое приглашение")
    await drop_message(cb, services)
    await _invite_menu(cb.message, services, client, code, new=False)


@router.callback_query(ClientCB.filter(F.action == "delete"))
async def client_delete_confirm(cb: CallbackQuery, callback_data: ClientCB, services):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    devices = await call(services.db.list_devices, client.id)
    lent = [d for d in devices if getattr(d, "is_lent", False)]
    await edit(cb, texts.client_delete_ask(client, devices, lent), kb.client_delete_confirm(client.id))
    await cb.answer()


@router.callback_query(ClientCB.filter(F.action == "delete_yes"))
async def client_delete_apply(cb: CallbackQuery, callback_data: ClientCB, services):
    target = await _client_or_alert(cb, services, callback_data.client_id)
    if target is None:
        return
    if target.tg_id == config.ADMIN_ID:
        await cb.answer("Профиль администратора нельзя удалить", show_alert=True)
        return
    res = await call(services.delete_client_with_devices, target.id)
    # снятые устройства сняты в любом случае: их держатели узнают сразу, иначе
    # при частичном отказе VPN у них просто гаснет без объяснений
    for holder_tg, d in res["removed"]:
        if holder_tg:
            await notify_one(cb.bot, holder_tg, texts.lent_device_deleted_by_admin_notice(d))
    if res["failed"]:
        await edit(cb, texts.CLIENT_DELETE_PARTIAL.format(
            name=texts._e(target.name), devices=texts._e(", ".join(res["failed"]))),
            kb.admin_client_back(target.id))
        await cb.answer("Сервер не ответил — профиль не удалён", show_alert=True)
        return
    await cb.answer()
    await edit(cb, texts.client_deleted_note(target.name, res["n"]), None)
    await send_menu(cb.message, services, *await clients_screen(services, cb.message.chat.id),
                    keep_id=cb.message.message_id)


# ─────────────────────────────────────────────────────────────────────────────
# Продление: срок сразу, «сохранить остаток» тумблером
# ─────────────────────────────────────────────────────────────────────────────

async def extend_screen(services, client_id: int, *, keep: bool = True, cancel_to=None):
    """Экран продления или None — профиля нет."""
    client = await call(services.db.get_client, client_id)
    if client is None:
        return None
    from awgbot.domain.services import SECONDS_PER_DAY
    cut_days = int(client.grace_pending_cut) // SECONDS_PER_DAY
    remainder = await call(services.remaining_for, client_id)
    return (texts.extend_text(client, cut_days, _bot(services)),
            kb.period_kb("extend", client_id, min_days=cut_days, keep=keep,
                         has_remainder=remainder > 0, cancel_cb=cancel_to))


@router.callback_query(ClientCB.filter(F.action.in_(("extend", "extend_exp"))))
async def extend_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    """extend_exp — из списка истекающих: после продления или отмены —
    обратно в список, пока он не пуст."""
    if callback_data.action == "extend_exp":
        await state.update_data(return_to="expiring")
    return_to = (await state.get_data()).get("return_to")
    screen = await extend_screen(
        services, callback_data.client_id,
        cancel_to=Menu(action="expiring").pack() if return_to == "expiring" else None)
    if screen is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    await edit(cb, *screen)
    await cb.answer()


@router.callback_query(PeriodCB.filter((F.ctx == "extend") & (F.kind == "keep_tgl")))
async def extend_keep_toggle(cb: CallbackQuery, callback_data: PeriodCB, services, state: FSMContext):
    return_to = (await state.get_data()).get("return_to")
    screen = await extend_screen(
        services, callback_data.ref, keep=bool(callback_data.keep),
        cancel_to=Menu(action="expiring").pack() if return_to == "expiring" else None)
    if screen is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    await edit(cb, *screen)
    await cb.answer()


@router.callback_query(PeriodCB.filter(F.ctx == "extend"))
async def extend_period_chosen(cb: CallbackQuery, callback_data: PeriodCB, services, state: FSMContext):
    """Срок выбран — продление сразу; остаток — по тумблеру в колбэке."""
    return_to = (await state.get_data()).get("return_to")
    await state.clear()
    keep = bool(callback_data.keep) and callback_data.kind != "never"
    await _do_extend(cb, services, callback_data.ref, callback_data.kind, keep=keep, return_to=return_to)


async def _do_extend(cb, services, client_id, kind, keep: bool, return_to: str | None = None):
    """Итог — след в чате двумя строками, следом — откуда пришли: список
    истекающих (пока не пуст) или карточка профиля."""
    try:
        result = await call(services.extend_period, client_id, kind, keep)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await send_notifications(cb.bot, result.notifications)
    fresh = await call(services.db.get_client, client_id)
    await cb.answer("Продлено")
    await edit(cb, texts.extended_note(fresh, kind, result.new_end, result.pause, _bot(services)), None)
    keep_id = cb.message.message_id
    if return_to == "expiring" and await call(services.expiring_subscriptions):
        await send_menu(cb.message, services, *await expiring_screen(services), keep_id=keep_id)
        return
    parts = await client_card_parts(services, client_id)
    if parts is None:
        await send_menu(cb.message, services, *await _panel_parts(services), keep_id=keep_id)
        return
    await send_menu(cb.message, services, *parts, keep_id=keep_id)


# ── админ снимает паузу клиента ──────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "resume_pause"))
async def admin_resume_pause(cb: CallbackQuery, callback_data: ClientCB, services):
    """Запасной выход из клиентской паузы (клиент заперся: Telegram только через
    этот VPN). Тот же exit_pause, что у клиента."""
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    ok, actual, new_end, notes = await call(services.exit_pause, client.id, auto=False)
    if not ok:
        await cb.answer("Профиль не на паузе", show_alert=True)
        await _show_client_card(cb, services, client.id, answer=False)
        return
    await send_notifications(cb.bot, notes)
    await cb.answer("Пауза снята")
    await edit(cb, texts.resumed_note(client, actual, new_end, _bot(services)), None)
    parts = await client_card_parts(services, client.id)
    if parts is not None:
        await send_menu(cb.message, services, *parts, keep_id=cb.message.message_id)
