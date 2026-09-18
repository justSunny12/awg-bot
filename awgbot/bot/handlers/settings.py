"""handlers/settings.py — экран «⚙️ Настройки» (только админ).

Значения хранятся в conf/*.yaml и меняются через settings.set_value → горячо,
без рестарта. Экран перерисовывается после каждого изменения и показывает
актуальные значения. Раздел под RoleFilter("admin"), как остальная админка.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.core import config
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import GwMarkCB, GwSlotCB, SetCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.states import GatewayToken, GatewayHome, GatewayLabel, MigrationPort
from awgbot.bot.handlers import settingscore as core
from awgbot.bot.notifier import send_notifications
from awgbot.bot.handlers.common import (call, edit, send_menu, show_main_menu,
                                        ask_tracked, cleanup_content)
from awgbot.domain.services import ServiceError

log = logging.getLogger("awgbot.settings")

router = Router(name="settings")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))


# ── рендер экранов ───────────────────────────────────────────────────────────
async def _screen(sec: str, services, key: str = ""):
    """(text, markup) для раздела sec.

    Корутина, а не обычная функция: разделы «upd» и «rt» ходят в БД и в
    self_check (тот при холодном кэше запускает ip/ipset/iptables). Синхронный
    вызов держал бы event loop на время рисования экрана — а рядом крутятся
    тик живости и polling. Все остальные разделы чисто текстовые, им await
    ничего не стоит.
    """
    if sec == "notify":
        return texts.SETTINGS_NOTIFY, kb.settings_notify()
    if sec == "ncl":
        return texts.SETTINGS_NOTIFY_CLIENTS, kb.settings_notify_clients()
    if sec == "email":
        acc = await call(services.email_account)
        return (texts.settings_email_text(acc, await call(services.email_last_check),
                                          settings.get_bool("email.resume_enabled", True),
                                          await call(services.email_resume_address)),
                kb.settings_email(acc is not None))
    if sec == "subs":
        return texts.SETTINGS_SUBS, kb.settings_subs()
    if sec == "srv":
        d = await call(services.server_screen)
        offer = (d.get("private_dns") or {}).get("mode") == "public"
        return (texts.settings_server_text(d),
                kb.settings_server(d.get("migration_blocked", ""), private_dns_offer=offer))
    if sec == "dns":
        info = await call(services.private_dns_info)
        blocked = bool(await call(services.migration_blocked_reason))
        return texts.private_dns_offer(info["target"]), kb.private_dns_choices(blocked)
    if sec == "mig_prep":
        d = await call(services.migration_prepare_data)
        if d["blocked"]:
            return (texts.settings_server_text(await call(services.server_screen)),
                    kb.settings_server(d["blocked"]))
        return texts.migration_prepare_intro(d), kb.migration_prepare_confirm()
    if sec == "fw":
        st = await call(services.firewall_screen)
        return texts.settings_firewall_text(st), kb.settings_firewall(st)
    if sec == "mon":
        return texts.SETTINGS_MON, kb.settings_mon()
    if sec == "backup":
        return texts.SETTINGS_BACKUP, kb.settings_backup(await call(services.backup_encryption_enabled))
    if sec == "svc":
        d = await call(services.svc_screen_data)          # один хоп вместо четырёх
        return (texts.settings_svc_text(d["state"], d["progress"], d["available"]),
                kb.settings_svc(d["state"], available=d["available"], orphans=d["orphans"]))
    if sec == "upd":
        return texts.settings_upd_text(), kb.settings_updates(await call(services.updates_muted))
    if sec == "rt":
        if not config.ROUTING_ENABLED and not await call(services.routing_provisioned):
            # Обвязки ещё нет — раздел и есть место, где её разворачивают.
            return texts.ROUTING_PROVISION_INTRO, kb.routing_provision()
        if not config.ROUTING_ENABLED:
            # Кнопку в этом случае не рисуем вовсе, но колбэк приходит и из
            # старого сообщения в истории чата. Открыть раздел, которого нет,
            # значит показать переключатели, ничего не делающие.
            return texts.SETTINGS_ROUTING_ABSENT, kb.settings_back()
        on = settings.get_bool("app.routing.enabled", False)
        status = await call(services.routing_status)
        text = texts.settings_routing_text(on, status)
        states = await call(services.gateway_states) if on else []
        if on:
            text += texts.settings_routing_gateway_block(states)
        return text, kb.settings_routing(on, states,
                                         can_add=len(states) < config.ROUTING_GATEWAYS_MAX)
    if sec == "rt_gw":
        if not config.ROUTING_ENABLED or not settings.get_bool("app.routing.enabled", False):
            return texts.SETTINGS_ROUTING_SUBOFF, kb.settings_back()
        cands = await call(services.gateway_candidates)
        slot = int(key or 0)
        states = await call(services.gateway_states)
        if slot:
            st = next((x for x in states if x["gateway"].id == slot), None)
            if st is None:
                return "Такого слота шлюза нет.", kb.settings_back("rt")
            return texts.gateway_replace_intro(st), kb.gateway_choose_kind(bool(cands), slot)
        if states:
            if len(states) >= config.ROUTING_GATEWAYS_MAX:
                return "Слоты шлюзов заняты: убери один, чтобы добавить другой.", kb.settings_back("rt")
            return texts.GATEWAY_STANDBY_CHOOSE_INTRO, kb.gateway_choose_kind(bool(cands), 0)
        return texts.GATEWAY_CHOOSE_INTRO, kb.gateway_choose_kind(bool(cands), 0)
    if sec in ("rt_lists", "rt_users", "rt_bundle", "rt_mon"):
        # Подразделы существуют только при включённой функции. Колбэк приходит
        # и из старого сообщения — тогда честно говорим, что раздел пуст.
        if not config.ROUTING_ENABLED or not settings.get_bool("app.routing.enabled", False):
            return texts.SETTINGS_ROUTING_SUBOFF, kb.settings_back()
        if sec == "rt_mon":
            info = await call(services.routing_monitor_info)
            return texts.routing_monitor_text(info), kb.settings_routing_monitor(info)
        if sec == "rt_bundle":
            # Промежуточный экран: файл уносит ключ линка, выпуск — осознанно.
            slot = int(key or 0)
            head = texts.ROUTING_BUNDLE_INTRO
            if slot:
                st = await call(services.gateway_state, slot)
                head = head.replace("Конфигурация шлюза</b>", f"Конфигурация шлюза {texts.slot_short(st)}</b>", 1)
            return head, kb.settings_routing_bundle(slot)
        if sec == "rt_lists":
            info = await call(services.routing_lists_info)
            return texts.routing_lists_text(info), kb.settings_routing_lists(info["every_hours"])
        clients = await call(services.routing_grantable_clients)
        return texts.routing_users_text(), kb.settings_routing_users(clients)
    return texts.SETTINGS_ROOT, kb.settings_root()


async def _render(cb: CallbackQuery, sec: str, services, key: str = ""):
    text, markup = await _screen(sec, services, key)
    await edit(cb, text, markup)


# Общая механика диалогов настроек (ввод, фраза, бэкап, почта, тумблер) — в
# settingscore; здесь только то, чем основной бот отличается: колбэки и
# клавиатуры.
HOOKS = core.Hooks(
    cancel_kb=kb.settings_cancel,
    email_offer_kb=kb.email_setup_offer,
    email_forget_kb=kb.email_forget_confirm,
    render=lambda cb, services, sec: _render(cb, sec, services),
    screen=lambda services, sec: _screen(sec, services),
)


async def _record(cb: CallbackQuery, text: str, services):
    """Оставить в чате СЛЕД события и вернуть раздел следующим сообщением.

    Начало, отмена и завершение переезда — из тех событий, о которых потом
    спрашивают «когда это было и чем кончилось». Ветка настроек живёт до
    следующей навигации и унесла бы ответ с собой: экран переписывается, и от
    итога не остаётся ничего.

    Кнопок на записи нет намеренно — иначе в чате оказалось бы два живых меню,
    и инвариант «одно активное» держать было бы нечем. Раздел приходит следом
    новым сообщением, как отчёт о рассылке.
    """
    await edit(cb, text, None)
    await send_menu(cb.message, services, *await _screen("svc", services),
                    keep_id=cb.message.message_id)


async def send_gw_bundle(message: Message, services, slot: int = 0) -> bool:
    """Собрать, зашифровать и отдать конфигурацию слота файлом с кнопкой
    «В меню». Одна точка для настроек, назначения шлюза и приёма токена."""
    try:
        blob, name = await call(services.gw_bundle_encrypted, slot or None)
    except (ServiceError, OSError) as e:
        await message.answer(f"⚠️ Конфигурация шлюза не собрана: {texts._e(str(e))}")
        return False
    from aiogram.types import BufferedInputFile
    await message.answer_document(
        BufferedInputFile(blob, filename=name),
        caption="⚙️ Конфигурация шлюза. Перешли файл боту шлюза — он проверит "
                "и применит сам.",
        reply_markup=kb.bundle_menu_kb())
    return True


def _slot_of(callback_data) -> int:
    return int(getattr(callback_data, "slot", 0) or 0)


@router.callback_query(GwMarkCB.filter(F.action == "pick_list"))
async def gateway_pick_list(cb: CallbackQuery, callback_data: GwMarkCB, services):
    slot = _slot_of(callback_data)
    cands = await call(services.gateway_candidates)
    await cb.answer()
    if not cands:
        await edit(cb, texts.GATEWAY_PICK_EMPTY, kb.gateway_choose_kind(False, slot))
        return
    await edit(cb, texts.GATEWAY_PICK_INTRO, kb.gateway_pick(cands, slot))


@router.callback_query(GwMarkCB.filter(F.action == "pick"))
async def gateway_pick(cb: CallbackQuery, callback_data: GwMarkCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await cb.answer()
    slot = _slot_of(callback_data)
    states = await call(services.gateway_states)
    prev = None
    replace_state = None
    if slot:
        replace_state = next((st for st in states if st["gateway"].id == slot), None)
        prev = replace_state.get("device") if replace_state else None
    await edit(cb, texts.gateway_mark_ask(dev, prev, standby=bool(states) and not slot,
                                          replace_state=replace_state),
               kb.gateway_mark_confirm(dev.id, slot))


@router.callback_query(GwMarkCB.filter(F.action == "mark_yes"))
async def gateway_mark_yes(cb: CallbackQuery, callback_data: GwMarkCB, services,
                           state: FSMContext):
    """Существующее устройство — всегда полный путь: ключи линка слота новые
    (устройство линка не знает, а прежняя машина теряет его сама), файл
    первого применения едет открытым и ставится руками, поэтому нужен токен
    агента слота. Та же дорога, что у нового устройства."""
    slot = _slot_of(callback_data)
    states = await call(services.gateway_states)
    token_slot = slot or (len(states) + 1)
    if not await call(services.gw_bot_token, token_slot):
        await cb.answer()
        await state.set_state(GatewayToken.value)
        await state.update_data(gw_device_id=callback_data.device_id, gw_slot=slot)
        await edit(cb, texts.gateway_ask_token(token_slot), kb.settings_cancel("rt_gw"))
        return
    await cb.answer("Назначаю…")
    await edit(cb, "🛰 Назначаю шлюз…", None)
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
    await _gateway_mark_go(cb.message, services, callback_data.device_id, slot)


@router.callback_query(GwMarkCB.filter(F.action == "new_ask"))
async def gateway_new_ask(cb: CallbackQuery, callback_data: GwMarkCB, services):
    await cb.answer()
    slot = _slot_of(callback_data)
    n = slot or (len(await call(services.db.gateways)) + 1)
    await edit(cb, texts.gateway_new_ask(n), kb.gateway_new_confirm(slot))


@router.callback_query(GwMarkCB.filter(F.action == "new_yes"))
async def gateway_new_yes(cb: CallbackQuery, callback_data: GwMarkCB, services, state: FSMContext):
    """Новая машина. Токен её бота спрашиваем ЗДЕСЬ и один раз на слот: он
    уедет внутрь файла первого применения, и установка на шлюзе не задаст ни
    одного вопроса. Токен уже есть — идём сразу к выпуску."""
    slot = _slot_of(callback_data)
    token_slot = slot or (len(await call(services.db.gateways)) + 1)
    if not await call(services.gw_bot_token, token_slot):
        await cb.answer()
        await state.set_state(GatewayToken.value)
        await state.update_data(gw_slot=slot)
        await edit(cb, texts.gateway_ask_token(token_slot), kb.settings_cancel("rt_gw"))
        return
    await cb.answer("Создаю устройство и ключи…")
    await _gateway_new_go(cb.message, services, slot)


@router.message(GatewayToken.value)
async def gateway_token_received(message: Message, state: FSMContext, services):
    token = (message.text or "").strip()
    try:
        await message.delete()          # токен в истории чата не держим — и
    except Exception:                   # noqa: BLE001
        pass                            # непринятый тоже: секрет есть секрет
    data = await state.get_data()
    slot = int(data.get("gw_slot") or 0)
    token_slot = slot or (len(await call(services.db.gateways)) + 1)
    try:
        await call(services.set_gw_bot_token, token, token_slot)
    except ServiceError as e:
        await message.answer(f"⚠️ {texts._e(str(e))}")
        return
    await state.clear()
    # Токен спрашивают из двух мест: «новая машина» и замена машины со сменой
    # ключей. Куда возвращаться, помнит state.
    device_id = data.get("gw_device_id")
    if device_id:
        await _gateway_mark_go(message, services, int(device_id), slot)
        return
    await _gateway_new_go(message, services, slot)


async def _gateway_mark_go(message: Message, services, device_id: int, slot: int = 0) -> None:
    """Назначить шлюзом существующее устройство со сменой ключей и отдать файл
    первого применения с инструкцией."""
    try:
        res = await call(services.gateway_setup, device_id, rekey=True, slot_id=slot or None)
    except ServiceError as e:
        await message.answer(f"⚠️ {texts._e(str(e))}")
        return
    await message.answer(texts.gateway_install_instructions(res["device"],
                                                            services.bundle_name(res["gateway"])))
    await _send_plain_bundle(message, services, res["gateway"].id)


async def _gateway_new_go(message: Message, services, slot: int = 0) -> None:
    """Создать устройство «Шлюз», выпустить файл первого применения и объяснить
    одну команду на машине-шлюзе."""
    try:
        res = await call(services.gateway_setup, None, slot_id=slot or None)
    except ServiceError as e:
        await message.answer(f"⚠️ {texts._e(str(e))}")
        return
    await message.answer(texts.gateway_install_instructions(res["device"],
                                                            services.bundle_name(res["gateway"])))
    await _send_plain_bundle(message, services, res["gateway"].id)


async def _send_plain_bundle(message: Message, services, slot: int = 0) -> None:
    """Открытый файл первого применения: внутри ключи и токен агента."""
    try:
        blob, name = await call(services.gw_bundle_plain, slot or None)
    except (ServiceError, OSError) as e:
        await message.answer(f"⚠️ Файл первого применения не собран: {texts._e(str(e))}")
        return
    from aiogram.types import BufferedInputFile
    await message.answer_document(
        BufferedInputFile(blob, filename=name),
        caption="🛰 Файл первого применения. Скопируй его на машину-шлюз в /root/ — "
                "установщик найдёт его сам. Внутри ключи и токен агента: после "
                "установки удали.",
        reply_markup=kb.bundle_menu_kb())


# ── слоты шлюзов (docs/gateway-failover.md §6) ───────────────────────────────

async def _slot_state(cb: CallbackQuery, services, slot: int, *, lazy_ping: bool = True):
    try:
        return await call(services.gateway_screen_state, slot, lazy_ping=lazy_ping)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return None


async def _render_card(cb: CallbackQuery, services, slot: int) -> None:
    st = await _slot_state(cb, services, slot)
    if st is None:
        return
    await edit(cb, texts.gateway_card_text(st, st["states"]),
               kb.gateway_card(st, back_to_list=len(st["states"]) > 1))


async def _render_list(cb: CallbackQuery, services) -> None:
    states = await call(services.gateway_states)
    switched = await call(services.db.get_state, services._RT_SWITCHED_KEY)
    auto = settings.get_bool("app.routing.failover.enabled", True)
    await edit(cb, texts.gateway_list_text(states, switched or "", auto),
               kb.gateway_list(states, can_add=len(states) < config.ROUTING_GATEWAYS_MAX,
                               failover_on=auto))


@router.callback_query(GwSlotCB.filter(F.action == "list"))
async def gw_slot_list(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await _render_list(cb, services)
    await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "card"))
async def gw_slot_card(cb: CallbackQuery, callback_data: GwSlotCB, services, state: FSMContext):
    await state.clear()
    await _render_card(cb, services, callback_data.slot)
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


@router.callback_query(GwSlotCB.filter(F.action == "pref"))
async def gw_slot_pref(cb: CallbackQuery, callback_data: GwSlotCB, services):
    """Галочка на месте: у ☑️ — перенести на этот слот, у ✅ — снять вовсе."""
    gw = await call(services.db.gateway, callback_data.slot)
    if gw is None:
        await cb.answer("Такого слота нет", show_alert=True)
        return
    await call(services.gateway_set_preferred, None if gw.preferred else gw.id)
    await _render_card(cb, services, gw.id)
    dev = await call(services.db.get_device, gw.device_id)
    await cb.answer("Предпочтительный: " + ("снят" if gw.preferred else (dev.name if dev else "этот шлюз")))


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
        from awgbot.bot.handlers.admin.devices import _device_card_parts
        dev = await call(services.db.get_device, gw.device_id)
        if dev is not None:
            await edit(cb, *await _device_card_parts(services, dev))
    else:
        await edit(cb, texts.gateway_card_text(st, st["states"]),
                   kb.gateway_card(st, back_to_list=len(st["states"]) > 1))
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
               kb.gateway_switch_confirm(st["gateway"].id, healthy))
    await cb.answer()


@router.callback_query(GwSlotCB.filter(F.action == "switch_yes"))
async def gw_slot_switch_yes(cb: CallbackQuery, callback_data: GwSlotCB, services):
    try:
        gw = await call(services.gateway_switch, callback_data.slot, manual=True)
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await _render_card(cb, services, gw.id)
    st = await call(services.gateway_screen_state, gw.id, lazy_ping=False)
    await cb.answer(f"Трафик идёт через {st['display']}".replace("«", "").replace("»", ""))


@router.callback_query(GwSlotCB.filter(F.action == "bundle"))
async def gw_slot_bundle(cb: CallbackQuery, callback_data: GwSlotCB, services):
    await cb.answer()
    await _render(cb, "rt_bundle", services, key=str(callback_data.slot or ""))


@router.callback_query(GwSlotCB.filter(F.action == "home"))
async def gw_slot_home(cb: CallbackQuery, callback_data: GwSlotCB, services, state: FSMContext):
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    await state.set_state(GatewayHome.value)
    await state.update_data(gw_slot=st["gateway"].id)
    await edit(cb, texts.gateway_home_text(st), kb.gateway_slot_cancel(st["gateway"].id))
    await cb.answer()


@router.message(GatewayHome.value)
async def gateway_home_received(message: Message, state: FSMContext, services):
    slot = int((await state.get_data()).get("gw_slot") or 0)
    await state.clear()
    try:
        res = await call(services.gateway_set_home_subnets, slot, message.text or "")
        st = await call(services.gateway_screen_state, slot, lazy_ping=False)
    except ServiceError as e:
        await message.answer(f"⚠️ {texts._e(str(e))}")
        return
    await message.answer(texts.gateway_home_report(res, st))
    await send_menu(message, services, texts.gateway_card_text(st, st["states"]),
                    kb.gateway_card(st, back_to_list=len(st["states"]) > 1))


@router.callback_query(GwSlotCB.filter(F.action == "label"))
async def gw_slot_label(cb: CallbackQuery, callback_data: GwSlotCB, services, state: FSMContext):
    st = await _slot_state(cb, services, callback_data.slot, lazy_ping=False)
    if st is None:
        return
    await state.set_state(GatewayLabel.value)
    await state.update_data(gw_slot=st["gateway"].id)
    await edit(cb, texts.gateway_label_text(st), kb.gateway_slot_cancel(st["gateway"].id))
    await cb.answer()


@router.message(GatewayLabel.value)
async def gateway_label_received(message: Message, state: FSMContext, services):
    slot = int((await state.get_data()).get("gw_slot") or 0)
    await state.clear()
    try:
        await call(services.gateway_set_label, slot, message.text or "")
        st = await call(services.gateway_screen_state, slot, lazy_ping=False)
    except ServiceError as e:
        await message.answer(f"⚠️ {texts._e(str(e))}")
        return
    await send_menu(message, services, texts.gateway_card_text(st, st["states"]),
                    kb.gateway_card(st, back_to_list=len(st["states"]) > 1))


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
        await edit(cb, "Шлюз и так не назначен.", kb.settings_back())
        return
    states = await call(services.gateway_states)
    now_active = next((x for x in states if x.get("active")), None)
    await edit(cb, texts.gateway_removed(prev, now_active), kb.settings_back())


@router.callback_query(GwMarkCB.filter(F.action == "remove_yes"))
async def gateway_remove_yes(cb: CallbackQuery, callback_data: GwMarkCB, services):
    await gw_slot_remove_yes(cb, GwSlotCB(action="remove_yes", slot=_slot_of(callback_data)), services)


# ── открытие раздела ─────────────────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "open"))
async def open_section(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    await state.clear()
    await _render(cb, callback_data.sec, services, callback_data.key or "")
    await cb.answer()


# ── тумблеры (bool в YAML или mute обновлений в БД) ───────────────────────────
@router.callback_query(SetCB.filter(F.act == "toggle"))
async def toggle(cb: CallbackQuery, callback_data: SetCB, services):
    key = callback_data.key
    if callback_data.sec == "upd" and key == "notify":
        # уведомления об обновлениях = мьют в БД (не YAML). never-расписание не
        # даёт включить (проверяем перед снятием мьюта).
        if str(settings.get("updates.poll_schedule", "day")).lower() == "never":
            await cb.answer("Сначала выбери расписание проверки (не «никогда»).", show_alert=True)
            return
        muted = await call(services.updates_muted)
        if muted:
            await call(services.unmute_updates)
        else:
            await call(services.mute_updates)
        await _render(cb, callback_data.sec, services)
        await cb.answer()
        return
    if key == "app.routing.enabled" and settings.get_bool(key, False):
        # Выключение бьёт по всем, кому фича разрешена, — только через
        # подтверждение; включение — сразу.
        await edit(cb, texts.ROUTING_DISABLE_CONFIRM, kb.routing_disable_confirm())
        await cb.answer()
        return

    async def _after_set(k):
        # выключатель условной маршрутизации меняет состояние системы, а не
        # только значение в yaml: применяем сразу, не дожидаясь тика монитора
        if k == "app.routing.enabled":
            await call(services.reconcile_routing)
            # Включили — тут же замер шлюза: пока фича была выключена, о его
            # состоянии молчали (и на старте тоже), и узнать, что он лежит,
            # админ должен сейчас, а не когда пожалуются люди.
            if settings.get_bool(k, False) and await call(services.db.gateways):
                ok, _reason = await call(services.routing_status)
                if ok:
                    warn = texts.routing_gateway_warning(await call(services.routing_probe),
                                                         at_start=False)
                    if warn:
                        await cb.message.answer(f"⚠️ {warn}", reply_markup=kb.hide_only())
    await core.toggle_bool(cb, services, HOOKS, key, callback_data.sec, after_set=_after_set)


@router.callback_query(SetCB.filter((F.sec == "rt") & (F.act == "do")))
async def routing_action(cb: CallbackQuery, callback_data: SetCB, services):
    """Разрешение профилю на РФ-доступ. Верхний слой флага: снимая его, гасим
    эффект, но настройки самого клиента не разрушаем."""
    if callback_data.key == "bundle":
        await cb.answer("Собираю и шифрую…")
        # Экран-инструкция гаснет: живым должно остаться одно меню, и это —
        # кнопка «В меню» на самом файле. Инструкцию помечаем как контент:
        # возврат в меню (show_main_menu → cleanup_content) удалит и её —
        # после ухода файла ей в чате делать нечего.
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                                  # noqa: BLE001
            pass
        await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
        await send_gw_bundle(cb.message, services, int(callback_data.val or 0))
        return
    if callback_data.key == "lists_refresh":
        # Колбэк отвечается ОДИН раз — второй ответ Telegram молча роняет.
        # Обновление занимает секунды, спиннер на кнопке их покрывает; итог —
        # числом в ответе, а свежесть видна в перерисованном блоке «Списки».
        n = await call(services.routing_update_lists, True)
        await _render(cb, "rt_lists", services)
        await cb.answer(f"В базовом наборе {n} записей.")
        return
    if callback_data.key == "off!":
        # подтверждённое выключение фичи целиком (см. toggle)
        try:
            await call(settings.set_value, "app.routing.enabled", False)
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await call(services.reconcile_routing)
        await _render(cb, "rt", services)
        await cb.answer("Условная маршрутизация выключена")
        return
    if callback_data.key == "bundle_menu":
        # Файл с бандлом уходит из чата целиком — после возврата он не нужен,
        # а внутри ключ линка. Панель — новым сообщением.
        try:
            await cb.message.delete()
        except Exception:                                  # noqa: BLE001
            pass
        await show_main_menu(cb.message, services, "admin")
        await cb.answer()
        return
    if callback_data.key != "allow":
        await cb.answer("Действие недоступно.", show_alert=True)
        return
    client = await call(services.db.get_client, int(callback_data.val or 0))
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    new_state = not client.routing_allowed
    notes = await call(services.set_routing_allowed, client.id, new_state)
    await send_notifications(cb.bot, notes)
    await _render(cb, "rt_users", services)
    await cb.answer(f"{client.name}: РФ-доступ "
                    + ("разрешён" if new_state else "запрещён"))


# ── свой DNS-резолвер: три решения ───────────────────────────────────────────
# Выше do_action по той же причине, что и остальные специфичные обработчики:
# его фильтр F.act == "do" перехватил бы sec="dns" и промолчал.
@router.callback_query(SetCB.filter((F.sec == "dns") & (F.act == "do")))
async def private_dns_action(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    key = callback_data.key
    if key == "now":
        # Решение записано, дальше — обычная подготовка переезда: она читает
        # его и говорит, что DNS клиентов станет своим.
        await call(services.set_private_dns_decision, "pending")
        blocked = await call(services.migration_blocked_reason)
        if blocked:
            await cb.answer(f"Сейчас переезд невозможен: {blocked}", show_alert=True)
            await _render(cb, "srv", services)
            return
        await state.clear()
        await _render(cb, "mig_prep", services)
        await cb.answer("Записал: следующий переезд — со своим резолвером")
        return
    if key == "later":
        await call(services.set_private_dns_decision, "pending")
        await edit(cb, texts.PRIVATE_DNS_LATER, kb.settings_back("srv"))
        await cb.answer()
        return
    if key == "never":
        await call(services.set_private_dns_decision, "dismissed")
        await edit(cb, texts.PRIVATE_DNS_DISMISSED, kb.settings_back("srv"))
        await cb.answer()
        return
    await cb.answer("Действие недоступно.", show_alert=True)


# ── ввод порта для переезда ──────────────────────────────────────────────────
# Регистрируется РАНЬШЕ общего edit_value: тот ловит любой act == "edit", а
# ключ "port" в SETTINGS_BOUNDS не значится — кнопка «Задать порт» упиралась бы
# в «Эта настройка недоступна».
@router.callback_query(SetCB.filter((F.sec == "mig_prep") & (F.act == "edit")))
async def migration_port_ask(cb: CallbackQuery, state: FSMContext, services):
    await state.set_state(MigrationPort.value)
    await core.ask(cb, services, texts.MIGRATION_ASK_PORT, kb.settings_cancel("mig_prep"))
    await cb.answer()


# ── ввод значения (FSM) ──────────────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "edit"))
async def edit_value(cb: CallbackQuery, callback_data: SetCB, state: FSMContext, services):
    await core.start_edit(cb, services, HOOKS, state, callback_data.key, callback_data.sec)


async def _migration_prepare(cb: CallbackQuery, services, want_port: str = "") -> None:
    """Поднять второй интерфейс под переезд и перезапустить бота: имя
    интерфейса читается при старте, без рестарта рычаг не появится."""
    await cb.answer("Поднимаю интерфейс…")
    await edit(cb, "🚚 Поднимаю второй интерфейс: ключи, порт, обфускация, "
                   "автозагрузка. Это несколько секунд.", None)
    try:
        res = await call(services.migration_prepare,
                         int(want_port) if str(want_port).isdigit() else None)
    except Exception as e:                                # noqa: BLE001
        await cb.message.answer(texts.migration_prepare_failed(str(e)))
        return
    sent = await cb.message.answer(texts.migration_prepared(res))
    await call(services.set_restart_wait, sent.chat.id, sent.message_id)
    try:
        await call(services.restart_bot)
    except OSError as e:
        log.warning("после подготовки переезда не удалось перезапустить бота: %s", e)
        await cb.message.answer(texts.migration_promote_restart_failed())


@router.message(MigrationPort.value)
async def migration_port_received(message: Message, state: FSMContext, services):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        await ask_tracked(message, services, "⚠️ Порт — число от 1 до 65535. Попробуй ещё раз.")
        return
    await state.clear()
    d = await call(services.migration_prepare_data, int(raw))
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, texts.migration_prepare_intro(d),
                    kb.migration_prepare_confirm(int(raw)))


async def _routing_provision(cb: CallbackQuery, services) -> None:
    """Развернуть обвязку условной маршрутизации. Долго (до минуты) и меняет
    состояние хоста, поэтому: сразу сказать, что идём, и показать итог."""
    await cb.answer("Разворачиваю…")
    await edit(cb, "🚀 Разворачиваю обвязку: dnsmasq, NAT, маршруты, линк. "
                   "Это до минуты — не нажимай ничего.", None)
    try:
        tail = await call(services.routing_provision)
    except ServiceError as e:
        await cb.message.answer(texts.routing_provision_failed(str(e)))
        return
    sent = await cb.message.answer(texts.routing_provisioned(tail))
    # Интерфейс линка читается при старте: без рестарта функция останется
    # спящей, а раздел — тем же экраном «не развёрнута».
    await call(services.set_restart_wait, sent.chat.id, sent.message_id)
    try:
        await call(services.restart_bot)
    except OSError as e:
        log.warning("после развёртывания не удалось перезапустить бота: %s", e)


async def _firewall_action(cb: CallbackQuery, callback_data: SetCB, services) -> None:
    """Действия раздела «Файервол». Включение и удаление адреса могут запереть
    вход, поэтому идут с таймером отката; подтверждает его человек ЗДЕСЬ, а не
    вторым SSH-сеансом: чат работает независимо от того, сломался SSH или нет."""
    key, val = callback_data.key, callback_data.val
    try:
        if key == "on":
            seconds = await call(services.firewall_enable)
            await cb.answer()
            await cb.message.answer(texts.firewall_armed(seconds))
        elif key == "off":
            await call(services.firewall_disable)
            await cb.answer("Фильтр снят")
        elif key == "confirm":
            await call(services.firewall_confirm)
            await cb.answer()
            await cb.message.answer(texts.firewall_confirmed())
        elif key == "rollback":
            await call(services.firewall_confirm)
            await call(services.firewall_disable)
            await cb.answer()
            await cb.message.answer(texts.firewall_rolled_back())
        elif key == "del":
            # val — номер записи в списке (см. keyboards.settings_firewall).
            # Список мог измениться с момента отрисовки: тогда честно скажем,
            # а не удалим соседа по сдвинувшемуся номеру.
            allow = (await call(services.firewall_screen)).get("raw_allow", [])
            idx = int(val) if val.isdigit() else -1
            if not 0 <= idx < len(allow):
                await cb.answer("Список изменился — открой раздел заново", show_alert=True)
                text, markup = await _screen("fw", services)
                await edit(cb, text, markup)
                return
            entry = allow[idx]
            await call(services.firewall_allow_remove, entry)
            await cb.answer(f"{entry} убран")
        else:
            await cb.answer("Действие недоступно", show_alert=True)
            return
    except ServiceError as e:
        await cb.answer(str(e)[:180], show_alert=True)
    except Exception as e:                                # noqa: BLE001
        log.warning("firewall %s: %s", key, e)
        await cb.answer(f"Не вышло: {e}"[:180], show_alert=True)
    text, markup = await _screen("fw", services)
    await edit(cb, text, markup)


# ── выбор enum (расписание обновлений) ───────────────────────────────────────
_RT_MON_PICKS = {
    "probe": ("app.routing.probe_seconds", ("30", "45", "60", "90")),
    "window": ("app.routing.failover.window_samples", ("5", "10", "20", "30")),
    "avail": ("app.routing.failover.min_availability", ("25", "50", "75")),
}


@router.callback_query(SetCB.filter(F.act == "pick"))
async def pick(cb: CallbackQuery, callback_data: SetCB, services):
    if callback_data.sec == "rt" and callback_data.key == "lists":
        hours = callback_data.val
        if hours not in ("3", "6", "12", "24"):
            await cb.answer("Нет такого варианта.", show_alert=True)
            return
        try:
            await call(settings.set_value, "app.routing.lists_refresh_hours", int(hours))
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await _render(cb, "rt_lists", services)
        await cb.answer()
        return
    if callback_data.sec == "rt" and callback_data.key in _RT_MON_PICKS:
        # мониторинг и резервирование: такт зонда, окно, порог — горячие ключи;
        # такт переставляет задачу планировщика сам (см. scheduler.HOT)
        setting, allowed = _RT_MON_PICKS[callback_data.key]
        if callback_data.val not in allowed:
            await cb.answer("Нет такого варианта.", show_alert=True)
            return
        try:
            await call(settings.set_value, setting, int(callback_data.val))
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await _render(cb, "rt_mon", services)
        await cb.answer()
        return
    if callback_data.sec == "backup" and callback_data.key == "channel":
        await core.set_backup_channel(cb, services, HOOKS, callback_data.val)
        return
    if callback_data.sec == "upd" and callback_data.key == "sched":
        opt = callback_data.val
        try:
            await call(settings.set_value, "updates.poll_schedule", opt)
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        if opt == "never":                      # никогда → авто-мьют уведомлений
            await call(services.mute_updates)
    await _render(cb, callback_data.sec, services)
    await cb.answer()


# ── действия (бэкап сейчас, рестарты, проверка обновлений) ────────────────────
# ── переезд профилей (docs/ROADMAP.md, п.3) ──────────────────────────────────
@router.callback_query(SetCB.filter((F.sec == "mig") & (F.act == "do")))
async def migration_action(cb: CallbackQuery, callback_data: SetCB, services):
    """Рычаг переезда и оба выхода.

    Ключ с восклицательным знаком — подтверждённое действие. Через
    подтверждение проходят все три: завершение и отмена необратимы по-разному,
    а старт меняет то, что получит КАЖДЫЙ следующий попросивший конфиг. Все
    трое обязаны показать последствия до нажатия, а не после.

    Итог каждого из трёх остаётся в чате отдельным сообщением (_record): экран
    настроек переписывается следующей навигацией, а «когда начали» и «чем
    кончилось» спрашивают потом.
    """
    key = callback_data.key
    if not await call(services.migration_available):
        await cb.answer("Переезд не настроен: пустые ключи в app.yaml.", show_alert=True)
        return
    # Сторож состояния. Колбэк приходит и из СТАРОГО сообщения в истории чата
    # (тот же класс, что у раздела маршрутизации): «finish!» с прошлогоднего
    # подтверждения, нажатый после отмены, снёс бы старые пиры орфанов и
    # заархивировал ровно то, что отмена сохранила.
    running = await call(services.migration_running)
    if key in ("pending", "finish", "cancel", "finish!", "cancel!") and not running:
        await cb.answer("Переезд сейчас не идёт — экран устарел.", show_alert=True)
        return
    if key in ("start", "start!") and running:
        # зеркальная половина сторожа: «start!» со старого подтверждения,
        # нажатый уже во время переезда, пересобрал бы выдачу вслепую
        await cb.answer("Переезд уже идёт — экран устарел.", show_alert=True)
        return

    if key == "start":
        clients, devices, to_birth = await call(services.migration_start_preview)
        await edit(cb, texts.migration_start_confirm(clients, devices, to_birth),
                   kb.migration_confirm("start"))
        await cb.answer()
        return

    if key == "start!":
        await cb.answer("Создаю новые профили…")
        res = await call(services.migration_start)
        await _record(cb, texts.migration_started(res), services)
        return

    if key == "pending":
        rows = await call(services.migration_pending)
        await edit(cb, texts.migration_pending_text(rows), kb.settings_back())
        await cb.answer()
        return

    if key == "orphans":
        rows = await call(services.migration_orphan_rows)    # имена одним проходом
        await edit(cb, texts.migration_orphans_text(rows), kb.settings_back())
        await cb.answer()
        return

    if key == "finish":
        _, dropped = await call(services.migration_finish_preview)
        await edit(cb, texts.migration_finish_confirm(dropped),
                   kb.migration_confirm("finish"))
        await cb.answer()
        return

    if key == "cancel":
        moved = len(await call(services.migration_moved_devices))
        await edit(cb, texts.migration_cancel_confirm(moved),
                   kb.migration_confirm("cancel"))
        await cb.answer()
        return

    if key == "finish!":
        await cb.answer("Завершаю…")
        removed, dropped, failed = await call(services.migration_finish)
        await _record(cb, texts.migration_finished(removed, dropped, failed), services)
        if not failed:
            # шлюзы получили двойников с новыми ключами — файлы сразу, по слоту
            for g in await call(services.db.gateways):
                await send_gw_bundle(cb.message, services, g.id)
        # Смена поколения могла ждать финала этого переезда (установщик её не
        # начинал, чтобы не подменить цель). Теперь очередь дошла.
        if not failed:
            from awgbot.infra import awglock
            if awglock.needs_migration() and not await call(services.migration_available):
                await cb.message.answer(texts.migration_generation_pending(),
                                        reply_markup=kb.migration_generation_pending())
        promoted = await call(services.pop_promoted_iface)
        if promoted:
            # Новый интерфейс стал основным. Деплой-значения читаются при старте,
            # поэтому рестарт здесь не косметика: без него бот продолжит считать
            # основным погашенный интерфейс и родит следующее устройство на нём.
            dns = (await call(services.private_dns_info))
            sent = await cb.message.answer(texts.migration_promoted(
                promoted, dns["dns1"] if dns["mode"] == "private" else ""))
            await call(services.set_restart_wait, sent.chat.id, sent.message_id)
            try:
                await call(services.restart_bot)
            except OSError as e:                       # systemd недоступен
                log.warning("после переезда не удалось перезапустить бота: %s", e)
                await cb.message.answer(texts.migration_promote_restart_failed())
        return

    if key == "cancel!":
        await cb.answer("Отменяю…")
        moved = await call(services.migration_cancel)
        await _record(cb, texts.migration_cancelled(moved), services)
        return

    await cb.answer("Действие недоступно.", show_alert=True)

# ── ♻️ Восстановление из файла в чате ────────────────────────────────────────
@router.callback_query(SetCB.filter((F.sec == "backup") & (F.act == "do") & (F.key.in_({"restore!", "restore_drop"}))))
async def backup_restore_action(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    from awgbot.bot.handlers import restore as rs
    if callback_data.key == "restore!":
        await rs.run_restore(cb, services, state)
    else:
        await rs.drop_restore(cb, state)


# ── 🔐 Шифрование бэкапов: фраза дважды (шаги — в settingscore) ─────────────
@router.callback_query(SetCB.filter((F.sec == "backup") & (F.act == "do") & (F.key == "enc_set")))
async def backup_passphrase_start(cb: CallbackQuery, state: FSMContext, services):
    await core.passphrase_start(cb, services, HOOKS, state)


# ── ✉️ E-mail: мастер подключения, проверка, отключение ─────────────────────
@router.callback_query(SetCB.filter((F.sec == "email") & (F.act == "do")))
async def email_action(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    if not await core.email_action(cb, services, HOOKS, state, callback_data.key):
        await cb.answer("Действие недоступно.", show_alert=True)


# Ввод значения, парольная фраза и мастер почты — общие обработчики сообщений.
_core = core.register(router, HOOKS, default_sec="root")
receive_value, backup_passphrase_first, backup_passphrase_second = (
    _core["receive_value"], _core["passphrase_first"], _core["passphrase_second"])
email_address, email_imap_host, email_imap_port = _core["address"], _core["imap_host"], _core["imap_port"]
email_smtp_host, email_smtp_port, email_password = _core["smtp_host"], _core["smtp_port"], _core["password"]


# ВЫШЕ do_action НАМЕРЕННО. Фильтры проверяются в порядке регистрации, а у
# do_action он широкий (F.act == "do") и перехватил бы sec="mig" целиком:
# ключ не подошёл бы ни к одной его ветке, функция закончилась бы молча —
# без ответа на колбэк, то есть с вечным спиннером на кнопке. По той же
# причине выше стоит и routing_action.
@router.callback_query(SetCB.filter(F.act == "do"))
async def do_action(cb: CallbackQuery, callback_data: SetCB, services):
    key = callback_data.key
    if callback_data.sec == "fw":
        await _firewall_action(cb, callback_data, services)
        return
    if callback_data.sec == "rt" and key == "provision":
        await _routing_provision(cb, services)
        return
    if callback_data.sec == "mig_prep" and key == "go":
        await _migration_prepare(cb, services, callback_data.val)
        return
    if key == "enc":                                   # экран шифрования
        mode = await call(services.backup_encryption_mode)
        await edit(cb, texts.backup_encryption_text(mode), kb.backup_encryption_kb(bool(mode)))
        await cb.answer()
        return
    if key == "now":                                   # бэкап сейчас
        await core.backup_now(cb, services, HOOKS)
        return
    if key in ("awg", "bot"):                          # сначала — цена действия
        await edit(cb, texts.SVC_CONFIRM_AWG if key == "awg" else texts.SVC_CONFIRM_BOT,
                   kb.svc_confirm(key))
        await cb.answer()
        return
    if key == "awg!":                                  # рестарт AWG
        await cb.answer("Перезапускаю AWG…")
        try:
            await call(services.restart_service)
            # клавиатура — полным рендером раздела: голый settings_svc() терял
            # бы кнопки переезда до следующего захода в раздел
            await edit(cb, "✅ AmneziaWG перезапущен, блокировки восстановлены.",
                       (await _screen("svc", services))[1])
        except Exception as e:                         # noqa: BLE001
            await edit(cb, f"⚠️ Ошибка перезапуска AWG: {e}",
                       (await _screen("svc", services))[1])
        return
    if key == "bot!":                                  # рестарт бота
        await cb.answer("Перезапускаю бота…")
        await edit(cb, "🔄 Бот перезапускается — вернётся через несколько секунд.", None)
        # Запоминаем ДО рестарта: обещание вернуться исполняет новый процесс,
        # подменяя это же сообщение панелью.
        await call(services.set_restart_wait, cb.message.chat.id, cb.message.message_id)
        await call(services.restart_bot)
        return
    if key == "check":                                 # проверить обновление сейчас
        await cb.answer("Проверяю…")
        nxt = await call(services.update_next)
        blocked = await call(services.update_block_reason, nxt) if nxt is not None else ""
        if nxt is None:
            await edit(cb, texts.update_current_ok(config.INSTALLED_VERSION),
                       kb.settings_updates(await call(services.updates_muted)))
        elif blocked:
            # Кнопку «Обновить» не показываем: она бы вела в отказ.
            await edit(cb, texts.update_blocked(nxt.tag, blocked),
                       kb.settings_updates(await call(services.updates_muted)))
        else:
            await edit(cb, texts.update_admin_available(config.INSTALLED_VERSION, nxt.tag, nxt.body, nxt.skipped),
                       kb.update_admin_available())
        return
