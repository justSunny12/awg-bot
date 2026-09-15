"""handlers/settings.py — экран «⚙️ Настройки» (только админ).

Значения хранятся в conf/*.yaml и меняются через settings.set_value → горячо,
без рестарта. Экран перерисовывается после каждого изменения и показывает
актуальные значения. Раздел под RoleFilter("admin"), как остальная админка.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, FSInputFile

from awgbot.core import config
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import GwMarkCB, SetCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.states import (BackupPassphrase, EmailSetup, GatewayToken,
                                MigrationPort, SettingsInput)
from awgbot.bot.handlers import mailwizard
from awgbot.bot.notifier import send_notifications
from awgbot.bot.handlers.common import (call, edit, send_menu, show_main_menu,
                                        ask_tracked, cleanup_content)
from awgbot.domain.services import ServiceError

log = logging.getLogger("awgbot.settings")

router = Router(name="settings")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))


# ── рендер экранов ───────────────────────────────────────────────────────────
async def _screen(sec: str, services):
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
        gw_state = await call(services.gateway_state) if on else {"device": None}
        if on:
            text += texts.settings_routing_gateway_line(gw_state)
        return text, kb.settings_routing(on, has_gateway=gw_state["device"] is not None)
    if sec == "rt_gw":
        if not config.ROUTING_ENABLED or not settings.get_bool("app.routing.enabled", False):
            return texts.SETTINGS_ROUTING_SUBOFF, kb.settings_back()
        cands = await call(services.gateway_candidates)
        return texts.GATEWAY_CHOOSE_INTRO, kb.gateway_choose_kind(bool(cands))
    if sec in ("rt_lists", "rt_users", "rt_bundle"):
        # Подразделы существуют только при включённой функции. Колбэк приходит
        # и из старого сообщения — тогда честно говорим, что раздел пуст.
        if not config.ROUTING_ENABLED or not settings.get_bool("app.routing.enabled", False):
            return texts.SETTINGS_ROUTING_SUBOFF, kb.settings_back()
        if sec == "rt_bundle":
            # Промежуточный экран: файл уносит ключ линка, выпуск — осознанно.
            return texts.ROUTING_BUNDLE_INTRO, kb.settings_routing_bundle()
        if sec == "rt_lists":
            info = await call(services.routing_lists_info)
            return texts.routing_lists_text(info), kb.settings_routing_lists(info["every_hours"])
        clients = await call(services.routing_grantable_clients)
        return texts.routing_users_text(), kb.settings_routing_users(clients)
    return texts.SETTINGS_ROOT, kb.settings_root()


async def _render(cb: CallbackQuery, sec: str, services):
    text, markup = await _screen(sec, services)
    await edit(cb, text, markup)


async def _after_input(message: Message, services, sec: str) -> None:
    """Раздел после ТЕКСТОВОГО ввода — новым сообщением через send_menu.

    Голый message.answer оставлял в чате два живых экрана: приглашение «введи
    значение» с кнопкой «Отмена» и новый раздел, а нав-указатель так и стоял на
    приглашении — следующий переход гасил не то. Служебное убираем: само
    приглашение, ввод человека, переспросы (всё это трекается); в чате
    остаются финишер «изменено: было → стало» и раздел."""
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, *await _screen(sec, services))


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


async def send_gw_bundle(message: Message, services) -> bool:
    """Собрать, зашифровать и отдать конфигурацию шлюза файлом с кнопкой
    «В меню». Одна точка для настроек, назначения шлюза и приёма токена."""
    try:
        blob, name = await call(services.gw_bundle_encrypted)
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


async def _deliver_bundle(cb: CallbackQuery, services, res: dict, headline: str) -> None:
    """Итог назначения: текст и сразу файл. Новые ключи → открытый файл для
    первого применения руками; ключи те же → шифрованный, для чата агента."""
    await edit(cb, headline, None)
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
    if res.get("rekeyed"):
        try:
            blob, name = await call(services.gw_bundle_plain)
        except (ServiceError, OSError) as e:
            await cb.message.answer(f"⚠️ Файл первого применения не собран: {texts._e(str(e))}")
            return
        from aiogram.types import BufferedInputFile
        await cb.message.answer_document(
            BufferedInputFile(blob, filename=name),
            caption="🛰 Файл первого применения: на машине-шлюзе `sudo sh awg-gw-bundle.sh`. "
                    "Внутри ключи — после применения удали.",
            reply_markup=kb.bundle_menu_kb())
    else:
        await send_gw_bundle(cb.message, services)


@router.callback_query(GwMarkCB.filter(F.action == "pick_list"))
async def gateway_pick_list(cb: CallbackQuery, services):
    cands = await call(services.gateway_candidates)
    await cb.answer()
    if not cands:
        await edit(cb, texts.GATEWAY_PICK_EMPTY, kb.gateway_choose_kind(False))
        return
    await edit(cb, texts.GATEWAY_PICK_INTRO, kb.gateway_pick(cands))


@router.callback_query(GwMarkCB.filter(F.action == "pick"))
async def gateway_pick(cb: CallbackQuery, callback_data: GwMarkCB, services):
    dev = await call(services.db.get_device, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await cb.answer()
    prev = await call(services.db.gateway_device)
    await edit(cb, texts.gateway_mark_ask(dev, prev), kb.gateway_mark_confirm(dev.id))


@router.callback_query(GwMarkCB.filter(F.action == "mark_yes"))
async def gateway_mark_yes(cb: CallbackQuery, callback_data: GwMarkCB, services,
                           state: FSMContext):
    """Существующее устройство. Шлюз уже был и это другая машина — ключи
    линка меняются, чтобы прежняя потеряла линк сама.

    Со сменой ключей файл первого применения едет открытым и ставится на
    машине с нуля — значит ему нужен токен агента ровно так же, как новой
    машине. Без него установка на шлюзе снова начинала задавать вопросы."""
    prev = await call(services.db.gateway_device)
    rekey = prev is not None and prev.id != callback_data.device_id
    if rekey and not await call(services.gw_bot_token):
        await cb.answer()
        await state.set_state(GatewayToken.value)
        await state.update_data(gw_device_id=callback_data.device_id)
        await edit(cb, texts.GATEWAY_ASK_TOKEN, kb.settings_cancel("rt_gw"))
        return
    await cb.answer("Назначаю…")
    try:
        res = await call(services.gateway_setup, callback_data.device_id, rekey=rekey)
    except ServiceError as e:
        await cb.message.answer(f"⚠️ {texts._e(str(e))}")
        return
    await _deliver_bundle(cb, services, res, texts.gateway_marked(res["device"], res["rekeyed"]))


@router.callback_query(GwMarkCB.filter(F.action == "new_ask"))
async def gateway_new_ask(cb: CallbackQuery, services):
    await cb.answer()
    await edit(cb, texts.GATEWAY_NEW_ASK, kb.gateway_new_confirm())


@router.callback_query(GwMarkCB.filter(F.action == "new_yes"))
async def gateway_new_yes(cb: CallbackQuery, services, state: FSMContext):
    """Новая машина. Токен её бота спрашиваем ЗДЕСЬ и один раз: он уедет внутрь
    файла первого применения, и установка на шлюзе не задаст ни одного
    вопроса. Токен уже есть — идём сразу к выпуску."""
    if not await call(services.gw_bot_token):
        await cb.answer()
        await state.set_state(GatewayToken.value)
        await edit(cb, texts.GATEWAY_ASK_TOKEN, kb.settings_cancel("rt_gw"))
        return
    await cb.answer("Создаю устройство и ключи…")
    await _gateway_new_go(cb.message, services)


@router.message(GatewayToken.value)
async def gateway_token_received(message: Message, state: FSMContext, services):
    token = (message.text or "").strip()
    try:
        await message.delete()          # токен в истории чата не держим — и
    except Exception:                   # noqa: BLE001
        pass                            # непринятый тоже: секрет есть секрет
    try:
        await call(services.set_gw_bot_token, token)
    except ServiceError as e:
        await message.answer(f"⚠️ {texts._e(str(e))}")
        return
    data = await state.get_data()
    await state.clear()
    # Токен спрашивают из двух мест: «новая машина» и смена шлюза со сменой
    # ключей. Куда возвращаться, помнит state.
    device_id = data.get("gw_device_id")
    if device_id:
        await _gateway_mark_go(message, services, int(device_id))
        return
    await _gateway_new_go(message, services)


async def _gateway_mark_go(message: Message, services, device_id: int) -> None:
    """Назначить шлюзом существующее устройство со сменой ключей и отдать файл
    первого применения с инструкцией."""
    try:
        res = await call(services.gateway_setup, device_id, rekey=True)
    except ServiceError as e:
        await message.answer(f"⚠️ {texts._e(str(e))}")
        return
    await message.answer(texts.gateway_install_instructions(res["device"]))
    await _send_plain_bundle(message, services)


async def _gateway_new_go(message: Message, services) -> None:
    """Создать устройство «Шлюз», выпустить файл первого применения и объяснить
    две команды на машине-шлюзе."""
    try:
        res = await call(services.gateway_setup, None)
    except ServiceError as e:
        await message.answer(f"⚠️ {texts._e(str(e))}")
        return
    await message.answer(texts.gateway_install_instructions(res["device"]))
    await _send_plain_bundle(message, services)


async def _send_plain_bundle(message: Message, services) -> None:
    """Открытый файл первого применения: внутри ключи и токен агента."""
    try:
        blob, name = await call(services.gw_bundle_plain)
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


@router.callback_query(GwMarkCB.filter(F.action == "remove_ask"))
async def gateway_remove_ask(cb: CallbackQuery, services):
    dev = await call(services.db.gateway_device)
    if dev is None:
        await cb.answer("Шлюз не назначен", show_alert=True)
        return
    await cb.answer()
    await edit(cb, texts.gateway_remove_ask(dev), kb.gateway_remove_confirm())


@router.callback_query(GwMarkCB.filter(F.action == "remove_yes"))
async def gateway_remove_yes(cb: CallbackQuery, services):
    await cb.answer("Убираю…")
    prev = await call(services.gateway_remove)
    if prev is None:
        await edit(cb, "Шлюз и так не назначен.", kb.settings_back())
        return
    await edit(cb, texts.gateway_removed(prev), kb.settings_back())


# ── открытие раздела ─────────────────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "open"))
async def open_section(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    await state.clear()
    await _render(cb, callback_data.sec, services)
    await cb.answer()


_TOGGLE_DEFAULTS = {"notifications.email_fallback": False}


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
    elif key == "notifications.email_fallback" and not settings.get_bool(key, False) \
            and not await call(services.email_configured):
        # включить нельзя без ящика — предложить настроить, не молча отказать
        await edit(cb, texts.EMAIL_NOT_CONFIGURED, kb.email_setup_offer("notify"))
        await cb.answer()
        return
    else:
        # дефолт тумблера — по ключу: у большинства «включено», но у ключей с
        # дефолтом «выключено» первое нажатие иначе записало бы «выкл»
        cur = settings.get_bool(key, _TOGGLE_DEFAULTS.get(key, True))
        try:
            await call(settings.set_value, key, not cur)
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        # выключатель условной маршрутизации меняет состояние системы, а не
        # только значение в yaml: применяем сразу, не дожидаясь тика монитора
        if key == "app.routing.enabled":
            await call(services.reconcile_routing)
    await _render(cb, callback_data.sec, services)
    await cb.answer()


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
        await send_gw_bundle(cb.message, services)
        return
    if callback_data.key == "lists_refresh":
        # Колбэк отвечается ОДИН раз — второй ответ Telegram молча роняет.
        # Обновление занимает секунды, спиннер на кнопке их покрывает; итог —
        # числом в ответе, а свежесть видна в перерисованном блоке «Списки».
        n = await call(services.routing_update_lists, True)
        await _render(cb, "rt_lists", services)
        await cb.answer(f"В базовом наборе {n} записей.")
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
    await _ask(cb, services, texts.MIGRATION_ASK_PORT, kb.settings_cancel("mig_prep"))
    await cb.answer()


async def _ask(cb: CallbackQuery, services, prompt: str, markup) -> None:
    """Приглашение к вводу — на месте экрана и в служебные: после ответа оно
    отслужило и убирается вместе с вводом (см. _after_input)."""
    await edit(cb, prompt, markup)
    await call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)


# ── ввод числового значения (FSM) ────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "edit"))
async def edit_value(cb: CallbackQuery, callback_data: SetCB, state: FSMContext, services):
    key = callback_data.key
    if key not in texts.SETTINGS_BOUNDS and key not in texts.SETTINGS_TEXT:   # старая/битая клавиатура
        await cb.answer("Эта настройка недоступна.", show_alert=True)
        return
    await state.set_state(SettingsInput.value)
    await state.update_data(key=key, sec=callback_data.sec)
    if key == "email.resume_address":
        prompt = texts.email_ask_resume_address(await call(services.email_resume_address))
    else:
        prompt = texts.settings_prompt(key)
    await _ask(cb, services, prompt, kb.settings_cancel(callback_data.sec))
    await cb.answer()


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


def _validate_server_value(key: str, raw: str) -> tuple[bool, str]:
    """Проверки для правок раздела «Сервер». Пускать сюда что угодно нельзя:
    значение уезжает в КАЖДУЮ следующую ссылку, а сломанную ссылку человек
    увидит только при импорте — и без единого сообщения об ошибке."""
    import ipaddress
    import re as _re
    if not raw:
        return False, "пусто — значение обязательно"
    if key == "app.network.server_host":
        try:
            ipaddress.ip_address(raw)
            return True, ""
        except ValueError:
            pass
        if _re.fullmatch(r"[0-9.]+", raw):
            # «10.8.1.300» — это опечатка в адресе, а не доменное имя: цифры и
            # точки проходят проверку имени, и ссылка уехала бы в никуда.
            return False, "похоже на IP с опечаткой — проверь октеты"
        if _re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
                         r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+", raw):
            return True, ""
        return False, "нужен IP или доменное имя"
    if key == "app.client_config.server_name":
        return (True, "") if len(raw) <= 64 else (False, "длинновато: не больше 64 символов")
    if key == "app.client_config.dns1":
        parts = [p for p in raw.replace(",", " ").split() if p]
        if not 1 <= len(parts) <= 2:
            return False, "один или два адреса"
        for p in parts:
            try:
                ipaddress.ip_address(p)
            except ValueError:
                return False, f"«{p}» не IP-адрес"
        return True, ""
    return True, ""


@router.message(SettingsInput.value)
async def receive_value(message: Message, state: FSMContext, services):
    data = await state.get_data()
    key, sec = data.get("key"), data.get("sec", "root")
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if key in texts.SETTINGS_TEXT:
        raw = (message.text or "").strip()
        if key == "email.resume_address":
            from awgbot.infra import mail
            if raw == "-":                            # «вернуть сам ящик»
                raw = ""
            if raw and not mail.is_address(raw):
                await ask_tracked(message, services, texts.EMAIL_BAD_ADDRESS)
                return
        elif key == "app.firewall.ssh_allow":
            # Вайтлист не «значение настройки», а список: пишет его сервис —
            # он же проверяет каждый адрес и перевыставляет таблицу.
            before = list(settings.get("app.firewall.ssh_allow", []) or [])
            try:
                after = await call(services.firewall_allow_add, raw)
            except ServiceError as e:
                await ask_tracked(message, services, f"⚠️ {texts._e(str(e))}")
                return
            await state.clear()
            await message.answer(texts.settings_ssh_allow_added(
                [x for x in after if x not in before] or [raw]))
            await _after_input(message, services, sec)
            return
        else:
            ok, err = _validate_server_value(key, raw)
            if not ok:
                await ask_tracked(message, services, f"⚠️ {texts._e(err)}")
                return
        old = str(settings.get(key, "") or "")
        shown_new = raw
        if key == "app.client_config.dns1":
            # В конфиге два поля, в UI одна строка. Второй адрес обязан
            # быть тем же, если назван один: стеки опрашивают список не
            # строго по порядку, и «публичный вторым номером» вернул бы
            # утечку резолва мимо нашего dnsmasq.
            parts = [x for x in raw.replace(",", " ").split() if x]
            old2 = str(settings.get("app.client_config.dns2", "") or "")
            old = f"{old}, {old2}" if old2 and old2 != old else old
            await call(settings.set_value, "app.client_config.dns2",
                       parts[1] if len(parts) > 1 else parts[0])
            raw = parts[0]
            shown_new = ", ".join(parts)
        elif key == "email.resume_address":
            old = old or "сам ящик"
            shown_new = raw or "сам ящик"
        try:
            await call(settings.set_value, key, raw)
        except settings.SettingsWriteError as e:
            await state.clear()
            await message.answer(str(e))
            await _after_input(message, services, sec)
            return
        await state.clear()
        await message.answer(texts.settings_changed(key, old, shown_new))
        await _after_input(message, services, sec)
        return
    if key not in texts.SETTINGS_BOUNDS:      # рассинхрон state (не должен случаться)
        await state.clear()
        await _after_input(message, services, sec)
        return
    lo, hi, _label, _unit = texts.SETTINGS_BOUNDS[key]
    raw = (message.text or "").strip()
    try:
        val = int(raw)
        if not (lo <= val <= hi):
            raise ValueError
    except ValueError:
        await ask_tracked(message, services, texts.settings_bad_value(key))
        return
    old = settings.get(key, None)
    try:
        await call(settings.set_value, key, val)
    except settings.SettingsWriteError as e:
        await state.clear()
        await message.answer(str(e))
        await _after_input(message, services, sec)
        return
    await state.clear()
    await message.answer(texts.settings_changed(key, old, val))
    await _after_input(message, services, sec)


# ── выбор enum (расписание обновлений) ───────────────────────────────────────
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
    if callback_data.sec == "backup" and callback_data.key == "channel":
        val = callback_data.val
        if val not in ("telegram", "email"):
            await cb.answer("Нет такого варианта.", show_alert=True)
            return
        if val == "email":
            if not await call(services.email_configured):
                await edit(cb, texts.EMAIL_NOT_CONFIGURED, kb.email_setup_offer("backup"))
                await cb.answer()
                return
            if not await call(services.backup_encryption_enabled):
                await cb.answer(texts.BACKUP_NEEDS_ENCRYPTION, show_alert=True)
                return
        try:
            await call(settings.set_value, "app.scheduler.backup_channel", val)
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await _render(cb, "backup", services)
        await cb.answer()
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
        if not failed and await call(services.db.gateway_device) is not None:
            # шлюз получил двойника с новыми ключами — файл сразу, без напоминаний
            await send_gw_bundle(cb.message, services)
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


# ── 🔐 Шифрование бэкапов: фраза дважды, сообщения удаляются ─────────────────
@router.callback_query(SetCB.filter((F.sec == "backup") & (F.act == "do") & (F.key == "enc_set")))
async def backup_passphrase_start(cb: CallbackQuery, state: FSMContext, services):
    from awgbot.bot.states import BackupPassphrase
    await state.clear()
    await state.set_state(BackupPassphrase.first)
    await _ask(cb, services, texts.BACKUP_ASK_PASSPHRASE, kb.settings_cancel("backup"))
    await cb.answer()


async def _take_secret_message(message: Message) -> str:
    text = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:                                  # noqa: BLE001
        pass
    return text


@router.message(BackupPassphrase.first)
async def backup_passphrase_first(message: Message, state: FSMContext, services):
    from awgbot.bot.states import BackupPassphrase
    from awgbot.domain.backupcrypto import MIN_PASSPHRASE_LEN
    phrase = await _take_secret_message(message)
    if len(phrase) < MIN_PASSPHRASE_LEN:
        await ask_tracked(message, services,
                          f"⚠️ Фраза короче {MIN_PASSPHRASE_LEN} символов. Пришли другую.")
        return
    await state.update_data(passphrase=phrase)
    await state.set_state(BackupPassphrase.second)
    await ask_tracked(message, services, texts.BACKUP_ASK_PASSPHRASE_AGAIN,
                      reply_markup=kb.settings_cancel("backup"))


@router.message(BackupPassphrase.second)
async def backup_passphrase_second(message: Message, state: FSMContext, services):
    from awgbot.bot.states import BackupPassphrase
    phrase = await _take_secret_message(message)
    first = (await state.get_data()).get("passphrase", "")
    if phrase != first:
        await state.set_state(BackupPassphrase.first)
        await state.update_data(passphrase="")
        await ask_tracked(message, services, texts.BACKUP_PASSPHRASE_MISMATCH,
                          reply_markup=kb.settings_cancel("backup"))
        return
    await state.clear()
    await call(services.backup_set_passphrase, phrase)
    await message.answer(texts.BACKUP_PASSPHRASE_SET)
    await _after_input(message, services, "backup")


# ── ✉️ E-mail: мастер подключения, проверка, отключение ─────────────────────
@router.callback_query(SetCB.filter((F.sec == "email") & (F.act == "do")))
async def email_action(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    from awgbot.infra import mail
    key = callback_data.key
    if key == "setup":
        await state.clear()
        await state.set_state(EmailSetup.address)
        acc = await call(services.email_account)
        prompt = texts.email_ask_address_change(acc.login) if acc else texts.EMAIL_ASK_ADDRESS
        await edit(cb, prompt, kb.settings_cancel("email"))
        await cb.answer()
        return
    if key == "check":
        await cb.answer("Проверяю…")
        ok, detail = await call(services.email_check)
        await _render(cb, "email", services)
        if not ok:
            await cb.message.answer(texts.email_check_failed(detail))
        return
    if key == "test":
        await cb.answer("Отправляю…")
        try:
            await call(services.email_send_test)
        except mail.MailError as e:
            await cb.message.answer(f"🔴 {e}")
            return
        acc = await call(services.email_account)
        await cb.message.answer(texts.email_test_sent(acc.login if acc else ""),
                                reply_markup=kb.hide_only())
        return
    if key == "forget":
        await edit(cb, texts.EMAIL_FORGET_CONFIRM, kb.email_forget_confirm())
        await cb.answer()
        return
    if key == "forget!":
        await call(services.email_forget)
        await cb.message.answer(texts.EMAIL_FORGOTTEN)
        await _render(cb, "email", services)
        await cb.answer()
        return
    await cb.answer("Действие недоступно.", show_alert=True)


# Мастер подключения ящика — общий модуль: те же шаги у агента шлюза.
_mw = mailwizard.register(router, cancel_kb=lambda: kb.settings_cancel("email"),
                          done_screen=lambda services: _screen("email", services))
email_address, email_imap_host, email_imap_port = _mw["address"], _mw["imap_host"], _mw["imap_port"]
email_smtp_host, email_smtp_port, email_password = _mw["smtp_host"], _mw["smtp_port"], _mw["password"]


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
        await cb.answer("Готовлю бэкап…")
        paths = await call(services.make_backup)
        if await call(services.backup_channel) == "email":
            from awgbot.infra import mail
            try:
                await call(services.email_send_backup, paths)
                acc = await call(services.email_account)
                await cb.message.answer(texts.backup_mailed(acc.login if acc else "", len(paths)),
                                        reply_markup=kb.hide_only())
            except mail.MailError as e:
                await cb.message.answer(f"🔴 {e}")
            await _render(cb, "backup", services)
            return
        for p in paths:
            try:
                await cb.message.answer_document(FSInputFile(p))
            except Exception:                          # noqa: BLE001
                pass
        await _render(cb, "backup", services)
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
