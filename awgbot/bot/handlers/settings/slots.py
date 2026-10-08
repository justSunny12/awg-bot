"""slots.py — карточки и действия слотов шлюзов."""

from __future__ import annotations

import logging
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import GwMarkCB, GwSlotCB
from awgbot.bot.states import GatewayHome, GatewayLabel
from awgbot.domain.services import ServiceError
from awgbot.bot.handlers.common import call, edit, send_menu, card_from_main, ask_here, ask_tracked, cleanup_content

log = logging.getLogger("awgbot.handlers.settings")
from ._router import router
from .gwmark import _slot_of
from .render import _render, card_kb, gateways_screen, send_gw_bundle

# ── слоты шлюзов ───────────────────────────────

async def _slot_state(cb: CallbackQuery, services, slot: int, *, lazy_ping: bool = True):
    try:
        return await call(services.gateway_screen_state, slot, lazy_ping=lazy_ping)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return None


async def _render_card(cb: CallbackQuery, services, slot: int) -> bool:
    """False — слота нет, alert уже дан (второй ответ его бы перекрыл)."""
    st = await _slot_state(cb, services, slot)
    if st is None:
        return False
    await edit(cb, texts.gateway_card_text(st, st["states"]), card_kb(st, cb.message.chat.id))
    return True


async def _render_list(cb: CallbackQuery, services) -> None:
    await edit(cb, *await gateways_screen(services))


@router.callback_query(GwSlotCB.filter(F.action == "list"))
async def gw_slot_list(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    card_from_main(cb.message.chat.id, False)      # с «Шлюзов» «Назад» карточки ведёт сюда
    await _render_list(cb, services)
    await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "card"))
async def gw_slot_card(cb: CallbackQuery, callback_data: GwSlotCB, services, state: FSMContext):
    await state.clear()
    if await _render_card(cb, services, callback_data.slot):
        await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "add"))
async def gw_slot_add(cb: CallbackQuery, services):
    await cb.answer()
    await _render(cb, "rt_gw", services)


@router.callback_query(GwSlotCB.filter(F.action == "failover"))
async def gw_slot_failover(cb: CallbackQuery, services):
    key = "app.routing.failover.enabled"
    try:
        await call(settings.set_value, key, not settings.get_bool(key, True))
    except settings.SettingsWriteError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await _render_list(cb, services)
    await cb.answer("Автопереключение " + ("включено" if settings.get_bool(key, True) else "выключено"))


@router.callback_query(GwSlotCB.filter(F.action == "peer_ask"))
async def gw_slot_peer_ask(cb: CallbackQuery, services):
    """«↔️ Связь подсетей»:
    диалог на месте списка. Включить можно и до того, как слоты готовы —
    инфобокс скажет, чего не хватает."""
    on = not await call(services.peer_nets_enabled)
    await edit(cb, texts.gateway_peer_ask(on), kb.gateway_peer_confirm(on))
    await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "peer_yes"))
async def gw_slot_peer_yes(cb: CallbackQuery, callback_data: GwSlotCB, services):
    # цель из кнопки; старая кнопка без цели (сообщение прежнего выпуска) — «не текущее»
    on = (callback_data.val == "1") if callback_data.val in ("0", "1") \
        else not await call(services.peer_nets_enabled)
    try:
        await call(services.set_peer_nets, on)
    except settings.SettingsWriteError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await _render_list(cb, services)
    await cb.answer(("Подсети связаны" if on else "Связь подсетей выключена")
                    + ": перевыпусти конфигурацию каждого шлюза", show_alert=True)


@router.callback_query(GwSlotCB.filter(F.action == "edit"))
async def gw_slot_edit(cb: CallbackQuery, callback_data: GwSlotCB, services, state: FSMContext):
    await state.clear()
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    await edit(cb, texts.gateway_edit_text(st), kb.gateway_edit_kb(st, two_slots=len(st["states"]) > 1))
    await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "pref"))
async def gw_slot_pref(cb: CallbackQuery, callback_data: GwSlotCB, services):
    """Галочка на месте: у ☑️ — перенести на этот слот, у ✅ — снять вовсе."""
    gw = await call(services.db.gateway, callback_data.slot)
    if gw is None:
        await cb.answer("Такого слота нет", show_alert=True)
        return
    await call(services.gateway_set_preferred, None if gw.preferred else gw.id)
    st = await _slot_state(cb, services, gw.id, lazy_ping=False)
    if st is not None:
        await edit(cb, texts.gateway_edit_text(st), kb.gateway_edit_kb(st, two_slots=len(st["states"]) > 1))
    dev = await call(services.db.get_device, gw.device_id)
    await cb.answer(("Предпочтительный: " + ("снят" if gw.preferred else (dev.name if dev else "этот шлюз")))[:190])


@router.callback_query(GwSlotCB.filter(F.action == "ping"))
async def gw_slot_ping(cb: CallbackQuery, callback_data: GwSlotCB, services):
    """Замер сейчас; карточка перерисовывается той, откуда нажали: из карточки
    устройства — она, из карточки слота — она."""
    gw = await call(services.db.gateway, callback_data.slot)
    if gw is None:
        await cb.answer("Такого слота нет", show_alert=True)
        return
    ms = await call(services.gateway_ping, gw.id)
    st = await call(services.gateway_screen_state, gw.id, lazy_ping=False)
    st["ping_ms"] = ms
    text = cb.message.text or cb.message.caption or ""
    if "Это устройство — шлюз" in text or "последний коннект" in text:
        from awgbot.bot.handlers.admin.devices import device_card_parts
        dev = await call(services.db.get_device, gw.device_id)
        if dev is not None:
            await edit(cb, *await device_card_parts(services, dev))
    else:
        await edit(cb, texts.gateway_card_text(st, st["states"]), card_kb(st, cb.message.chat.id))
    await cb.answer(texts.ping_line(ms).replace("<code>", "").replace("</code>", "") if ms is not None
                    else f"Шлюз {st['display']} не отвечает", show_alert=ms is None)


@router.callback_query(GwSlotCB.filter(F.action == "switch_ask"))
async def gw_slot_switch_ask(cb: CallbackQuery, callback_data: GwSlotCB, services):
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    if st.get("active"):
        await cb.answer("Трафик уже идёт через этот шлюз")
        return
    current = next((x for x in st["states"] if x.get("active")), None)
    healthy = bool(st.get("link_ok"))
    await edit(cb, texts.gateway_switch_ask(st, current, healthy),
               kb.gateway_switch_confirm(st["gateway"].id, healthy, from_list=callback_data.val == "l"))
    await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "switch_yes"))
async def gw_slot_switch_yes(cb: CallbackQuery, callback_data: GwSlotCB, services):
    try:
        gw = await call(services.gateway_switch, callback_data.slot, manual=True)
    except ServiceError as e:
        await cb.answer(str(e)[:190], show_alert=True)
        return
    if callback_data.val == "l":
        await _render_list(cb, services)
    else:
        await _render_card(cb, services, gw.id)
    st = await call(services.gateway_screen_state, gw.id, lazy_ping=False)
    await cb.answer(f"Трафик идёт через {st['display']}".replace("«", "").replace("»", "")[:190])


@router.callback_query(GwSlotCB.filter(F.action == "lan_ask"))
async def gw_slot_lan_ask(cb: CallbackQuery, callback_data: GwSlotCB, services):
    """«🔀 VPN-транзит»: диалог на месте карточки.
    Без локальной подсети включать нечего — alert, не диалог."""
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    gw = st["gateway"]
    on = not gw.lan_mode
    if on and not gw.home_subnets:
        await cb.answer(texts.GATEWAY_LAN_NO_SUBNET, show_alert=True)
        return
    resolver = await call(services.gateway_resolver_addr, gw) if on else ""
    await edit(cb, texts.gateway_lan_ask(st, on, resolver), kb.gateway_lan_confirm(gw.id, on))
    await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "lan_yes"))
async def gw_slot_lan_yes(cb: CallbackQuery, callback_data: GwSlotCB, services):
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    on = (callback_data.val == "1") if callback_data.val in ("0", "1") else not st["gateway"].lan_mode
    if on == bool(st["gateway"].lan_mode):
        await cb.answer(texts.already_state(on))
        await _render_card(cb, services, callback_data.slot)
        return
    try:
        await call(services.gateway_set_lan_mode, callback_data.slot, on)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await _render_card(cb, services, callback_data.slot)
    online = bool((st.get("channel") or {}).get("online"))
    tail = "применится автоматически по управляющему каналу" if online else "перевыпусти конфигурацию шлюза"
    if online and await call(services.peer_nets_enabled):
        # режим доедет каналом, а подсети соседей меняются у обоих шлюзов и
        # живут в конфиге линка — это только файлом
        tail += "; для связи подсетей перевыпусти конфигурацию каждого шлюза"
    await cb.answer(("VPN-транзит включён: " if on else "VPN-транзит выключен: ") + tail, show_alert=True)


@router.callback_query(GwSlotCB.filter(F.action == "router"))
async def gw_slot_router(cb: CallbackQuery, callback_data: GwSlotCB, services):
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    gw = st["gateway"]
    tab = callback_data.val or "mt"
    dev = st.get("device")
    title = (dev.name if dev is not None else f"слот {gw.id}") + (f" ({gw.label})" if gw.label else "")
    await edit(cb, texts.gateway_router_text(title, gw.home_subnets[0] if gw.home_subnets else "",
                                             peer_nets=st.get("peer_nets") or [], tab=tab),
               kb.gateway_router_kb(gw.id, tab))
    await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "bundle"))
async def gw_slot_bundle(cb: CallbackQuery, callback_data: GwSlotCB, services):
    """«📤 Конфигурация» в карточке: файл сразу, без экрана «что
    произойдёт». Карточка гаснет и помечается как контент: живым остаётся
    «В меню» на файле, а «В меню» и итог применения с шлюза уберут её вместе
    с файлом (send_gw_bundle запоминает обе)."""
    await cb.answer("Собираю и шифрую…")
    await _issue_bundle_here(cb, services, int(callback_data.slot or 0))


async def _issue_bundle_here(cb: CallbackQuery, services, slot: int) -> None:
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                      # noqa: BLE001
        pass
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
    await send_gw_bundle(cb.message, services, slot, instr_id=cb.message.message_id)


@router.callback_query(GwSlotCB.filter(F.action == "home"))
async def gw_slot_home(cb: CallbackQuery, callback_data: GwSlotCB, services, state: FSMContext):
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    await state.set_state(GatewayHome.value)
    await state.update_data(gw_slot=st["gateway"].id)
    # приглашение — в служебные (ask_here): после ответа оно отслужило и
    # убирается вместе с вводом; «✖️ Отмена» возвращает карточку слота на место
    await ask_here(cb, services, state, texts.gateway_home_text(st), "gw", st["gateway"].id)
    await cb.answer()


@router.message(GatewayHome.value)
async def gateway_home_received(message: Message, state: FSMContext, services):
    slot = int((await state.get_data()).get("gw_slot") or 0)
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    try:
        res = await call(services.gateway_set_home_subnets, slot, message.text or "")
        st = await call(services.gateway_screen_state, slot, lazy_ping=False)
    except ServiceError as e:
        await ask_tracked(message, services, f"⚠️ {texts._e(str(e))}")   # ввод открыт, «Отмена» на месте
        return
    await state.clear()
    await cleanup_content(message.bot, services, message.chat.id)
    from awgbot.bot import screens
    await send_menu(message, services,
                    screens.with_note(texts.gateway_card_text(st, st["states"]), texts.gateway_home_report(res, st)),
                    card_kb(st, message.chat.id))


async def gateway_edit_screen(services, slot: int):
    """(текст, клавиатура) подэкрана «✏️ Изменить» слота — для реестра экранов."""
    try:
        st = await call(services.gateway_screen_state, slot, lazy_ping=False)
    except ServiceError:
        return None
    return texts.gateway_edit_text(st), kb.gateway_edit_kb(st, two_slots=len(st["states"]) > 1)


@router.callback_query(GwSlotCB.filter(F.action == "name"))
async def gw_slot_name(cb: CallbackQuery, callback_data: GwSlotCB, services, state: FSMContext):
    """«✏️ Имя» слота — переименование устройства-шлюза с возвратом в
    «✏️ Изменить» (общий приём EditDeviceName в handlers/admin/devices.py)."""
    from awgbot.bot.states import EditDeviceName
    from awgbot.bot.handlers.common import ask_here
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    dev = st.get("device")
    if dev is None:
        await cb.answer("Устройство слота не найдено", show_alert=True)
        return
    await state.set_state(EditDeviceName.value)
    await ask_here(cb, services, state, texts.device_name_prompt(dev.name), "gwedit", st["gateway"].id,
                   device_id=dev.id)
    await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "label"))
async def gw_slot_label(cb: CallbackQuery, callback_data: GwSlotCB, services, state: FSMContext):
    from awgbot.bot.handlers.common import ask_here
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    await state.set_state(GatewayLabel.value)
    await ask_here(cb, services, state, texts.gateway_label_text(st), "gwedit", st["gateway"].id,
                   gw_slot=st["gateway"].id)
    await cb.answer()


@router.message(GatewayLabel.value)
async def gateway_label_received(message: Message, state: FSMContext, services):
    from awgbot.bot.handlers.common import back_to_context
    data = await state.get_data()
    slot = int(data.get("gw_slot") or 0)
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    gw = await call(services.db.gateway, slot)
    old = gw.label if gw is not None else ""
    try:
        await call(services.gateway_set_label, slot, message.text or "")
        gw = await call(services.db.gateway, slot)
    except ServiceError as e:
        await ask_tracked(message, services, f"⚠️ {texts._e(str(e))}")
        return
    await state.clear()
    new = gw.label if gw is not None else ""
    note = f"✅ Подпись: {texts._e(old) or '—'} → {texts._e(new) or '—'}"
    await back_to_context(message, services, data, "admin", note=note)


async def _remove_ask(cb: CallbackQuery, services, slot: int) -> None:
    st = await _slot_state(cb, services, slot, lazy_ping=False)
    if st is None:
        return
    dev = st.get("device")
    if dev is None:
        await cb.answer("Устройство слота не найдено", show_alert=True)
        return
    other = next((x for x in st["states"] if x["gateway"].id != slot), None)
    await cb.answer()
    await edit(cb, texts.gateway_remove_ask(dev, state=st, other=other),
               kb.gateway_remove_confirm(slot))


@router.callback_query(GwSlotCB.filter(F.action == "remove_ask"))
async def gw_slot_remove_ask(cb: CallbackQuery, callback_data: GwSlotCB, services):
    await _remove_ask(cb, services, callback_data.slot)


@router.callback_query(GwMarkCB.filter(F.action == "remove_ask"))
async def gateway_remove_ask(cb: CallbackQuery, callback_data: GwMarkCB, services):
    """«🛑 Не шлюз?» из карточки устройства и старые сообщения без слота."""
    slot = _slot_of(callback_data)
    if not slot and callback_data.device_id:
        gw = await call(services.db.gateway_by_device, callback_data.device_id)
        slot = gw.id if gw else 0
    if not slot:
        first = await call(services.gateway_first_slot)
        if first is None:
            await cb.answer("Шлюз не назначен", show_alert=True)
            return
        slot = first.id
    await _remove_ask(cb, services, slot)


@router.callback_query(GwSlotCB.filter(F.action == "remove_yes"))
async def gw_slot_remove_yes(cb: CallbackQuery, callback_data: GwSlotCB, services):
    await cb.answer("Убираю…")
    prev = await call(services.gateway_remove, callback_data.slot or None)
    if prev is None:
        await edit(cb, "Шлюз и так не назначен", kb.settings_back("rt"))
        return
    states = await call(services.gateway_states)
    now_active = next((x for x in states if x.get("active")), None)
    from awgbot.bot import screens
    card_from_main(cb.message.chat.id, False)
    text, markup = await gateways_screen(services)
    note = texts.gateway_removed(prev, now_active)
    if await call(services.gw_bot_token, callback_data.slot or None):
        note += "\n" + texts.GW_TOKEN_NOT_FORGOTTEN        # запись env не удалась — журнал молчать не должен
    await edit(cb, screens.with_note(text, note), markup)


@router.callback_query(GwMarkCB.filter(F.action == "remove_yes"))
async def gateway_remove_yes(cb: CallbackQuery, callback_data: GwMarkCB, services):
    await gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=_slot_of(callback_data)), services)
