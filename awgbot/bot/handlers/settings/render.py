"""render.py — экраны ролевых разделов основного бота (_screen — его
br.role_screens; общие разделы рисует bot/sections) и сообщения о файле
конфигурации шлюза."""

from __future__ import annotations

import logging
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from awgbot.core import config
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.bot.roles import MAIN
from awgbot.bot import keyboards as kb
from awgbot.domain.services import ServiceError
from awgbot.util import bundlecrypt
from awgbot.bot.handlers.common import send_menu_to
from awgbot.bot.handlers.common import call, edit, send_menu, card_is_from_main, edit_nav

log = logging.getLogger("awgbot.handlers.settings")

# ── рендер экранов ───────────────────────────────────────────────────────────
async def _screen(sec: str, services, key: str = ""):
    """(text, markup) ролевого раздела sec; общие разделы обеих ролей — в
    bot/sections/ (sections.screen), сюда они не приходят.

    Корутина, а не обычная функция: разделы «rt» ходят в БД и в
    self_check (тот при холодном кэше запускает ip/ipset/iptables). Синхронный
    вызов держал бы event loop на время рисования экрана — а рядом крутятся
    тик живости и polling. Все остальные разделы чисто текстовые, им await
    ничего не стоит.
    """
    from awgbot.bot import sections
    if sections.available(MAIN, sec):
        return await sections.screen(sec, MAIN, services, key)
    if sec == "subs":
        return texts.settings_subs_text(), kb.settings_subs()
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
        from awgbot.bot import paging
        return texts.settings_firewall_text(st), kb.settings_firewall(
            st, page=paging.page_of(config.ADMIN_ID, "fw"))
    if sec == "rt":
        return await gateways_screen(services)
    if sec == "rt_gw":
        if not config.ROUTING_ENABLED or not settings.get_bool("app.routing.enabled", False):
            return texts.SETTINGS_ROUTING_SUBOFF, kb.settings_back("rt")
        cands = await call(services.gateway_candidates)
        slot = int(key or 0)
        states = await call(services.gateway_states)
        if slot:
            st = next((x for x in states if x["gateway"].id == slot), None)
            if st is None:
                return "Такого слота шлюза нет", kb.settings_back("rt")
            return texts.gateway_replace_intro(st), kb.gateway_choose_kind(bool(cands), slot)
        if states:
            if len(states) >= config.ROUTING_GATEWAYS_MAX:
                return "Слоты шлюзов заняты: убери один, чтобы добавить другой", kb.settings_back("rt")
            return texts.GATEWAY_STANDBY_CHOOSE_INTRO, kb.gateway_choose_kind(bool(cands), 0)
        return texts.GATEWAY_CHOOSE_INTRO, kb.gateway_choose_kind(bool(cands), 0)
    if sec in ("rt_lists", "rt_users", "rt_mon", "rt_params"):
        # Подразделы существуют только при включённой функции. Колбэк приходит
        # и из старого сообщения — тогда честно говорим, что раздел пуст.
        if not config.ROUTING_ENABLED or not settings.get_bool("app.routing.enabled", False):
            return texts.SETTINGS_ROUTING_SUBOFF, kb.settings_back("rt")
        if sec in ("rt_mon", "rt_lists", "rt_params"):
            info = await call(services.routing_monitor_info)
            lists = await call(services.routing_lists_info)
            return texts.routing_params_text(info, lists), kb.routing_params_kb(info, lists["every_hours"])
        clients = await call(services.routing_grantable_clients)
        from awgbot.bot import paging
        return texts.routing_users_text(), kb.settings_routing_users(
            clients, page=paging.page_of(config.ADMIN_ID, "rtusers"))
    return await sections.screen("root", MAIN, services)


async def gateways_screen(services):
    """Экран «🛰 Шлюзы» с главной: не развёрнуто / спит до перезапуска /
    выключено / слоты со строками состояния."""
    if not config.ROUTING_ENABLED and not await call(services.routing_provisioned):
        return texts.ROUTING_PROVISION_INTRO, kb.gateways_kb((), provisioned=False)
    if not config.ROUTING_ENABLED:
        # обвязка есть, интерфейс линка читается при старте — до перезапуска
        # функция спит; колбэк приходит и из старого сообщения
        return texts.SETTINGS_ROUTING_ABSENT, kb.gateways_kb((), awake=False)
    if not settings.get_bool("app.routing.enabled", False):
        return texts.GATEWAYS_OFF, kb.gateways_kb((), enabled=False)
    states = await call(services.gateway_states)
    status = await call(services.routing_status)
    switched = await call(services.db.get_state, services._RT_SWITCHED_KEY)
    auto = settings.get_bool("app.routing.failover.enabled", True)
    peer = await call(services.gateway_peer_nets_info) if len(states) > 1 else None
    lists = await call(services.routing_lists_info) if states else None
    text = texts.gateways_text(states, status=status, switched_at=switched or "", auto_on=auto,
                               peer_info=peer, lists=lists)
    return text, kb.gateways_kb(states, can_add=len(states) < config.ROUTING_GATEWAYS_MAX,
                                failover_on=auto, peer_nets_on=(peer["enabled"] if peer else None))


async def _render(cb: CallbackQuery, sec: str, services, key: str = ""):
    text, markup = await _screen(sec, services, key)
    await edit(cb, text, markup)


async def _render_nav(cb: CallbackQuery, sec: str, services, key: str = ""):
    """Раздел после диалога settingscore — через edit_nav: запасной путь edit()
    (сообщение удалено или слишком старое) шлёт новое, и оно обязано стать
    живым меню, иначе в чате два меню."""
    text, markup = await _screen(sec, services, key)
    await edit_nav(cb, services, text, markup)


async def _shared(cb: CallbackQuery, callback_data, services, state=None) -> bool:
    """Колбэк общего раздела — диспетчеру bot/sections (сюда он приходит
    только из тестов, которые зовут прежние обработчики напрямую); True —
    обработан."""
    from awgbot.bot import sections as secs
    packed = secs.resolve(MAIN, callback_data.pack())
    if packed is None:
        return False
    await secs.handle(cb, packed, services, state, MAIN)
    return True


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
    from awgbot.bot import sections
    await send_menu(cb.message, services, *await sections.screen("svc", MAIN, services),
                    keep_id=cb.message.message_id)


async def send_gw_bundle(message: Message, services, slot: int = 0, instr_id: int | None = None) -> bool:
    """Собрать, зашифровать и отдать конфигурацию слота файлом с кнопкой
    «В меню». Одна точка для карточки, назначения шлюза и приёма токена.
    instr_id — сообщение над файлом (погасшая карточка): «В меню» и итог
    применения с шлюза уберут оба."""
    try:
        blob, name = await call(services.gw_bundle_encrypted, slot or None)
    except (ServiceError, OSError) as e:
        await message.answer(ui.fail("Конфигурация шлюза не собрана", str(e)))
        return False
    from aiogram.types import BufferedInputFile
    display, agent_bot = await call(services.gw_bundle_target, slot or None)
    slot_id = await call(services.gw_slot_id, slot or None)
    if slot_id:
        # прежний файл этого слота, если ещё в чате, — вон: ключ линка в нём
        # тот же, а итог придёт только на новый
        await _drop_bundle_msgs(message.bot, services, slot_id)
    sent = await message.answer_document(
        BufferedInputFile(blob, filename=name),
        caption=texts.gateway_bundle_caption(display, agent_bot),
        reply_markup=kb.bundle_menu_kb(slot_id))
    if slot_id:
        await call(services.gw_bundle_msg_set, slot_id, message.chat.id, sent.message_id, instr_id,
                   bundlecrypt.fingerprint(blob))
    return True


def _bundle_chat(where: dict) -> int:
    return int(where.get("chat") or config.ADMIN_ID)


async def _bundle_display(services, slot_id: int) -> tuple[str, dict]:
    """(«имя» слота для подписи, бот шлюза); слота уже нет — «слот N»."""
    try:
        return await call(services.gw_bundle_target, slot_id)
    except (ServiceError, OSError):
        return f"слот {slot_id}", {}


async def _drop_bundle_msgs(bot, services, slot_id: int, fp: str = "", where: dict | None = None) -> bool:
    """Убрать из чата выданный файл слота с его инструкцией и забыть о них.
    fp задан — только если это тот самый файл: итог о другом (чужом или
    прежнем) файле новый не трогает. Вернуть, было ли что убирать."""
    if where is None:
        where = await call(services.gw_bundle_msg_get, slot_id)
    if not where:
        return False
    if fp and where.get("fp") and where.get("fp") != fp:
        return False
    chat_id = _bundle_chat(where)
    for mid in (where.get("instr"), where.get("file")):
        if mid:
            try:
                await bot.delete_message(chat_id, int(mid))
            except Exception:                              # noqa: BLE001
                pass
    await call(services.gw_bundle_msg_clear, slot_id)
    return True


async def bundle_applied(bot, services, slot_id: int, ok: bool, error: str, fp: str = "") -> None:
    """Итог применения пришёл каналом. При любом исходе файл отслужил (внутри
    ключ линка): он и сообщение над ним уходят из чата; следом уведомление об
    итоге и карточка слота, как по «В меню» — после отказа файл перевыпускают
    оттуда же. Итог о другом файле (fp не сошёлся) или без файла в чате —
    только запись, без уведомления."""
    where = await call(services.gw_bundle_msg_get, slot_id)
    chat_id = _bundle_chat(where)
    if not await _drop_bundle_msgs(bot, services, slot_id, fp, where):
        return
    # уведомление об итоге — раз чат админа затронут; следом карточка слота
    display, _bot = await _bundle_display(services, slot_id)
    sent = await bot.send_message(chat_id, texts.gateway_bundle_applied_text(display, ok, error))
    await call(services.db.add_content_msg_id, chat_id, sent.message_id)
    await _show_card_anew(bot, services, chat_id, slot_id)


async def _show_card_anew(bot, services, chat_id: int, slot_id: int) -> None:
    """Карточка слота новым сообщением как живое меню чата — без входящего
    сообщения (по событию канала). Слота уже нет — главная."""
    try:
        st = await call(services.gateway_screen_state, slot_id)
    except ServiceError:
        st = None
    if st is None:
        from awgbot.bot.handlers.admin import _panel_parts
        text, markup = await _panel_parts(services)
    else:
        text, markup = texts.gateway_card_text(st, st["states"]), card_kb(st, chat_id)
    await send_menu_to(bot, services, chat_id, text, markup)


async def bundle_installed(bot, services, slot_id: int) -> None:
    """Агент нового шлюза впервые вышел на связь каналом: админу «✅ Шлюз …
    успешно настроен» со ссылкой на бота шлюза — без кнопок, как контент.
    Файл первого применения и инструкция, если ещё в чате, уходят, и тогда
    следом новым сообщением идёт карточка слота — куда вела «В меню» на файле,
    единственная живая кнопка. Файл уже убрали — меню выше и так живое,
    второго не нужно."""
    where = await call(services.gw_bundle_msg_get, slot_id)
    chat_id = _bundle_chat(where)
    dropped = False
    if where.get("plain"):
        # шифрованный файл, выпущенный после файла первого применения, — не
        # этого события: его уберёт итог применения
        dropped = await _drop_bundle_msgs(bot, services, slot_id, where=where)
    display, agent_bot = await _bundle_display(services, slot_id)
    sent = await bot.send_message(chat_id, texts.gateway_installed_text(display, agent_bot))
    await call(services.db.add_content_msg_id, chat_id, sent.message_id)
    if dropped:
        await _show_card_anew(bot, services, chat_id, slot_id)



def card_kb(st: dict, chat_id: int | None) -> InlineKeyboardMarkup:
    """Клавиатура карточки слота: выход на главную, если карточку открыли
    ссылкой оттуда (common.card_from_main), иначе в список или раздел."""
    return kb.gateway_card(st, back_to_list=len(st["states"]) > 1,
                           back_main=card_is_from_main(chat_id))
