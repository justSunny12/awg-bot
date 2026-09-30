"""
handlers/gateway.py — панель и операции агента шлюза (роль gateway, только админ).

Панель, «🩺 Здоровье», «🔧 Восстановить» (реассерт через подтверждение),
экран «🔀 VPN-транзит» со своими списками и рецептом роутера, настройки агента
(перезапуски AWG и бота — в их корне, через подтверждение), «🛡 SSH-доступ»,
обновления, приём шифрованного бандла файлом. Пробы и операции блокирующие
(subprocess) — через call, как всё синхронное в проекте.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.core import config
from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB, HideCB, UpdateCB
from awgbot.bot.states import GatewayTransitDomain
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers import settingscore as core
from awgbot.bot.handlers.common import (call, edit_nav, send_menu, cleanup_content, purge_menus, ask_here,
                                        dismiss_update_reports, forget_secret, ask_tracked,
                                        drop_previous_nav)
from awgbot.bot.states import SshPort, GwSshAllow
from awgbot.domain.gwssh import SshOwnerRefusal
from awgbot.domain.services import ServiceError
from awgbot.util import bundlecrypt

log = logging.getLogger("awgbot.gateway")

router = Router(name="gateway")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

# Бандл ~25 КБ; всё, что заметно крупнее, — не наш файл. Лимит спасает от
# скачивания случайно пересланного видео.
_BUNDLE_MAX_BYTES = 512 * 1024


def _snapshot_max_age() -> float:
    """Снимок тика годится для панели, пока не старше двух тиков: пропущенный
    тик — уже повод сходить живьём, а не показывать позавчерашнее."""
    from awgbot.core import settings
    return 2 * 60 * settings.get_int("app.gateway.monitor_minutes", 3)


async def _status(services, fresh: bool):
    """fresh — живой снимок (и он же сохраняется для следующих показов);
    иначе снимок последнего тика, если свежий, а нет — живой."""
    if not fresh:
        st = await call(services.cached_status, _snapshot_max_age())
        if st is not None:
            return st
        return await call(services.snapshot)
    await call(services.invalidate_static)               # «Статус»: и статику живьём
    return await call(services.snapshot)


async def _panel(target, services, cb: CallbackQuery | None = None, fresh: bool = False,
                 keep_id=None):
    """Панель — через нав-хелперы: одно живое меню в чате, прошлое гаснет,
    история ведётся для /start."""
    st = await _status(services, fresh)
    lan = bool(getattr(st, "lan", None))
    tag = await call(services.update_available_tag)
    if cb is not None:
        await edit_nav(cb, services, texts.gateway_panel(st, tag), kb.gateway_panel_kb(lan))
    else:
        await send_menu(target, services, texts.gateway_panel(st, tag), kb.gateway_panel_kb(lan),
                        keep_id=keep_id)


async def restore_panel_after_restart(bot, services) -> None:
    """Исполнить обещание «вернётся через несколько секунд» — как у основного:
    обещание подменяется отчётом и остаётся в чате, панель — следующим
    сообщением. Зовётся новым процессом на старте."""
    waiting = await call(services.pop_restart_wait)
    if waiting is None:
        return
    chat_id, mid = waiting
    try:
        await bot.edit_message_text(texts.BOT_RESTARTED, chat_id=chat_id,
                                    message_id=mid, reply_markup=None)
    except Exception:                                  # noqa: BLE001
        pass
    from awgbot.bot.handlers.common import _dismiss_previous_nav
    await _dismiss_previous_nav(bot, services, chat_id)
    st = await _status(services, fresh=False)
    sent = await bot.send_message(chat_id, texts.gateway_panel(st, await call(services.update_available_tag)),
                                  reply_markup=kb.gateway_panel_kb(bool(getattr(st, "lan", None))))
    await call(services.db.nav_touch, chat_id, sent.message_id)


_FIRST_PANEL_KEY = "gw_first_panel_sent"


async def send_first_panel(bot, services) -> None:
    """Первый запуск после установки: панель админу сама, если диалог уже
    есть — при опознании кодом админ боту писал, и установщик обещает «бот
    напишет сам». Из файла первого применения диалога нет: Telegram не даёт
    боту начать первым, отправка не проходит — молчим, панель придёт на
    /start, как и сказано в инструкции."""
    from awgbot.core import config
    if await call(services.db.get_state, _FIRST_PANEL_KEY):
        return
    try:
        st = await _status(services, fresh=False)
        sent = await bot.send_message(config.ADMIN_ID, texts.gateway_panel(st, await call(services.update_available_tag)),
                                      reply_markup=kb.gateway_panel_kb(bool(getattr(st, "lan", None))))
    except Exception:                                  # noqa: BLE001
        return                                         # диалога ещё нет
    await call(services.db.nav_touch, config.ADMIN_ID, sent.message_id)
    await call(services.db.set_state, _FIRST_PANEL_KEY, "1")


@router.message(CommandStart())
async def gw_start(message: Message, services, state: FSMContext):
    await state.clear()
    await call(services.db.set_state, _FIRST_PANEL_KEY, "1")   # диалог есть — первая панель не нужна
    await purge_menus(message.bot, services, message.chat.id)
    await _panel(message, services)


@router.callback_query(GwCB.filter(F.action == "panel"))
async def gw_panel(cb: CallbackQuery, services, state: FSMContext):
    await cb.answer()
    await state.clear()
    await _panel(cb.message, services, cb)


@router.callback_query(GwCB.filter(F.action == "refresh"))
async def gw_refresh(cb: CallbackQuery, services, state: FSMContext):
    """«Обновить» — единственная кнопка, которая всегда ходит по пробам живьём."""
    await state.clear()
    await cb.answer("Снимаю показания…")
    await _panel(cb.message, services, cb, fresh=True)


@router.callback_query(GwCB.filter(F.action == "health"))
async def gw_health(cb: CallbackQuery, services):
    await cb.answer("Проверяю…")
    await call(services.invalidate_static)               # монитор здоровья — всё живьём
    st = await call(services.snapshot)         # с сохранением: панель и здоровье из одного момента
    await edit_nav(cb, services, texts.gateway_health(st), kb.gateway_health_kb())


@router.callback_query(GwCB.filter(F.action == "settings"))
async def gw_settings(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await edit_nav(cb, services, texts.gw_settings_text(), kb.gateway_settings_kb())
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "maint"))
async def gw_maint(cb: CallbackQuery, services, state: FSMContext):
    """«Обслуживание» из старого меню — теперь корень настроек."""
    await gw_settings(cb, services, state)


# ── разделы настроек: уведомления / мониторинг / резервное копирование ───────

def _email_section(services):
    acc = services.email_account()
    return (texts.settings_email_text(acc, services.email_last_check()),
            kb.gateway_email_kb(acc is not None))


def _ssh_section(services):
    from awgbot.bot import paging
    st = services.ssh_screen()
    return texts.gateway_ssh_text(st), kb.gateway_ssh_kb(st, page=paging.page_of(config.ADMIN_ID, "gwssh"))


def _backup_section(services):
    from awgbot.core import settings as _settings
    enc = services.backup_encryption_enabled()
    return (texts.settings_backup_text(enc, str(_settings.get("app.scheduler.backup_channel", "telegram") or "")),
            kb.gateway_backup_kb(enc))


_SECTIONS = {
    "notify": lambda services: (texts.gw_settings_notify_text(), kb.gateway_notify_kb()),
    "ssh": _ssh_section,
    "email": _email_section,
    "mon": lambda services: (texts.gw_settings_mon_text(), kb.gateway_mon_kb()),
    "backup": _backup_section,
}


async def _section(services, sec: str):
    return await call(_SECTIONS[sec], services)


async def _render(cb: CallbackQuery, services, sec: str) -> None:
    await edit_nav(cb, services, *await _section(services, sec))


# Общая механика диалогов настроек — в settingscore; здесь только колбэки и
# клавиатуры агента.
HOOKS = core.Hooks(
    email_offer_kb=kb.gateway_email_offer,
    email_forget_kb=kb.gateway_email_forget_confirm,
    render=_render,
    screen=_section,
    gateway=True,
)


@router.callback_query(GwCB.filter(F.action.in_(set(_SECTIONS))))
async def gw_section(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    await state.clear()
    await edit_nav(cb, services, *await _section(services, callback_data.action))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "enc"))
async def gw_encryption(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    mode = await call(services.backup_encryption_mode)
    await edit_nav(cb, services, texts.backup_encryption_text(mode, gateway=True),
                   kb.gateway_encryption_kb(bool(mode)))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "enc_set"))
async def gw_encryption_set(cb: CallbackQuery, services, state: FSMContext):
    await core.passphrase_start(cb, services, HOOKS, state)


def _section_of(key: str) -> str:
    if (key.startswith("quiet_hours.") or key.startswith("resource_alerts.")
            or key.startswith("notifications.") or key == "app.gateway.temp_alert_c"):
        return "notify"
    if key == "backup_when" or key.startswith("app.scheduler.backup_"):
        return "backup"
    return "mon"


@router.callback_query(GwCB.filter(F.action == "tgl"))
async def gw_toggle(cb: CallbackQuery, callback_data: GwCB, services):
    """Тумблер bool в conf — та же механика, что у основного бота."""
    key = callback_data.val
    await core.toggle_bool(cb, services, HOOKS, key, _section_of(key))


@router.callback_query(GwCB.filter(F.action == "edit"))
async def gw_edit(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    key = callback_data.val
    await core.start_edit(cb, services, HOOKS, state, key, _section_of(key))


@router.callback_query(GwCB.filter(F.action == "backup!"))
async def gw_backup_now(cb: CallbackQuery, services):
    await core.backup_now(cb, services, HOOKS)


@router.callback_query(GwCB.filter(F.action == "bk_ch"))
async def gw_backup_channel(cb: CallbackQuery, callback_data: GwCB, services):
    await core.set_backup_channel(cb, services, HOOKS, callback_data.val)


@router.callback_query(GwCB.filter(F.action == "cyc"))
async def gw_cycle(cb: CallbackQuery, callback_data: GwCB, services):
    """Кнопка-цикл: канал бэкапа (с проверками почты и шифрования) и
    расписание проверки обновлений; итог — всплывашкой."""
    from awgbot.core import settings
    key = callback_data.val
    if key == "app.scheduler.backup_channel":
        cur = str(settings.get(key, "telegram") or "telegram").lower()
        await core.set_backup_channel(cb, services, HOOKS, "email" if cur == "telegram" else "telegram")
        return
    if key == "updates.poll_schedule":
        cur = str(settings.get(key, "day")).lower()
        cyc = list(kb.UPDATE_SCHEDULE_CYCLE)
        new = cyc[(cyc.index(cur) + 1) % len(cyc)] if cur in cyc else cyc[0]
        try:
            await call(settings.set_value, key, new)
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await cb.answer(texts.cycle_toast(key, new))
        await _updates_screen(cb, services, scan=False)
        return
    await cb.answer("Кнопка устарела — открой раздел заново", show_alert=True)


# ── ✉️ E-mail у агента: ящик из бандла или руками, проверка, отключение ─────

_EMAIL_KEYS = {"em_setup": "setup", "em_check": "check", "em_test": "test",
               "em_forget": "forget", "em_forget!": "forget!"}


@router.callback_query(GwCB.filter(F.action.in_(set(_EMAIL_KEYS))))
async def gw_email_action(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    await core.email_action(cb, services, HOOKS, state, _EMAIL_KEYS[callback_data.action])


# ── бандл файлом ─────────────────────────────────────────────────────────────

@router.message(F.document)
async def gw_bundle_document(message: Message, services, state: FSMContext):
    doc = message.document
    from awgbot.bot.handlers import restore as rs
    if rs.looks_like_backup(doc):                     # резервная копия, не конфигурация
        await rs.offer_restore(message, services, state, gateway=True)
        return
    name = (getattr(doc, "file_name", "") or "").lower()
    if name.startswith("awg-gw-bundle") and name.endswith(".sh"):
        # открытый файл первого применения: внутри ключи линка и аплинка,
        # токен агента — в чате ему не место, а применяют его на устройстве
        await forget_secret(message)
        await message.answer(texts.GW_FIRST_RUN_FILE)
        return
    if doc.file_size and doc.file_size > _BUNDLE_MAX_BYTES:
        await message.answer(texts.GW_BUNDLE_NOT_OURS)
        return
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    blob = buf.getvalue()
    # Формат проверяем ДО предложения применить, расшифровку — только при
    # применении: чужой файл отбивается сразу, а ключ линка читается один раз.
    if not blob.startswith(bundlecrypt.MAGIC):
        if blob.startswith(b"#!/bin/sh") and b"awg-gw-bundle" in blob[:4096]:
            await forget_secret(message)
            await message.answer(texts.GW_FIRST_RUN_FILE)
            return
        await message.answer(texts.GW_BUNDLE_NOT_OURS)
        return
    # Файл у нас в памяти — в чате ему делать нечего. Основной бот свою копию
    # убирает по «В меню», а пересланная жила в переписке с агентом вечно:
    # внутри ключ линка, токен агента, фраза шифрования копий, пароль почты.
    await forget_secret(message)
    await state.update_data(bundle=base64.b64encode(blob).decode())
    # осмотр до вопроса: изменится ли конфиг линка — от этого зависит, будет
    # ли обрыв и нужно ли о нём предупреждать. Не расшифровался — скажем при
    # применении, там текст ошибки полный.
    info = await call(services.inspect_bundle, blob)
    link_changed = bool(info.get("link_changed", True)) if info.get("ok") else True
    # Вопрос «применить?» — новое живое меню; прежнюю панель удаляем целиком:
    # без кнопок над итогом применения она только занимала бы экран.
    await drop_previous_nav(message.bot, services, message.chat.id)
    await send_menu(message, services,
                    texts.gateway_bundle_received(link_changed, await call(services.carries_traffic)),
                    kb.gateway_bundle_kb())


# Ввод значения, парольная фраза и мастер почты — общие обработчики сообщений.
_core = core.register(router, HOOKS, default_sec="mon")
gw_receive_value, gw_passphrase_first, gw_passphrase_second = (
    _core["receive_value"], _core["passphrase_first"], _core["passphrase_second"])


# ── 🛡 Доступ по SSH ─────────────────────────────────────────────────────────
# Та же механика, что в разделе основного бота (handlers/settings.py): ввод
# порта — FSM, «тот же» и «занят» — финишер с выбором, результат — со «Скрыть»
# и раздел следом. Сверх того — отказ при чужом владельце sshd_config.

@router.callback_query(GwCB.filter(F.action.in_({"ssh_port", "ssh_port_retry"})))
async def gw_ssh_port_ask(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    st = await call(services.ssh_screen)
    if st.get("owner"):
        # Чужой владелец — отказ сразу по кнопке, без ввода: у целевых машин
        # (OMV) это основной случай, лишний шаг ни к чему.
        await state.clear()
        await edit_nav(cb, services,
                       texts.gateway_ssh_owner_refusal(st, None if st.get("sshd_down") else st["port"]),
                       kb.gateway_back_kb("ssh"))
        await cb.answer()
        return
    await state.set_state(SshPort.value)
    if callback_data.action == "ssh_port_retry":
        # с финишера: он остаётся в чате с одной «Скрыть», приглашение — новым
        try:
            await cb.message.edit_reply_markup(reply_markup=kb.hide_only())
        except Exception:                                 # noqa: BLE001
            pass
        await state.update_data(ctx_kind="set_ssh", ctx_ref=0)
        await send_menu(cb.message, services, texts.gw_ssh_port_ask(st.get("port")), kb.cancel_input("set_ssh"))
    else:
        await ask_here(cb, services, state, texts.gw_ssh_port_ask(st.get("port")), "set_ssh")
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "ssh_port_back"))
async def gw_ssh_port_back(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.hide_only())
    except Exception:                                     # noqa: BLE001
        pass
    await send_menu(cb.message, services, *await _section(services, "ssh"))
    await cb.answer()


@router.message(SshPort.value)
async def gw_ssh_port_received(message: Message, state: FSMContext, services):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        await ask_tracked(message, services, "⚠️ Порт — число от 1 до 65535, попробуй ещё раз")
        return
    port = int(raw)
    st = await call(services.ssh_screen)
    if st.get("owner"):
        # Чужой владелец — отказ экраном (текст длинный), ввод закрыт.
        await state.clear()
        await cleanup_content(message.bot, services, message.chat.id)
        await send_menu(message, services,
                        texts.gateway_ssh_owner_refusal(st, None if st.get("sshd_down") else st["port"]),
                        kb.gateway_back_kb("ssh"))
        return
    if not st.get("sshd_down") and port == int(st.get("port") or 0):
        await state.clear()
        await cleanup_content(message.bot, services, message.chat.id)
        await send_menu(message, services, texts.ssh_port_same(port), kb.gateway_ssh_port_finisher_kb())
        return
    try:
        busy = await call(services.ssh_port_busy, port)
    except ServiceError as e:
        await ask_tracked(message, services, f"⚠️ {texts._e(str(e))}")
        return
    if busy:
        await state.clear()
        await cleanup_content(message.bot, services, message.chat.id)
        await send_menu(message, services, texts.ssh_port_busy(port, "" if busy == "?" else busy),
                        kb.gateway_ssh_port_finisher_kb())
        return
    await state.clear()
    try:
        old = await call(services.ssh_port_change, port)
    except SshOwnerRefusal as e:
        await cleanup_content(message.bot, services, message.chat.id)
        st = await call(services.ssh_screen)
        await send_menu(message, services, texts.gateway_ssh_owner_refusal(st, e.listening),
                        kb.gateway_back_kb("ssh"))
        return
    except ServiceError as e:
        await core.after_input(message, services, HOOKS, "ssh", f"⚠️ Порт не изменён: {texts._e(str(e))}")
    else:
        await core.after_input(message, services, HOOKS, "ssh", texts.gateway_ssh_port_changed(old, port))


@router.callback_query(GwCB.filter(F.action == "ssh_add"))
async def gw_ssh_allow_ask(cb: CallbackQuery, services, state: FSMContext):
    await state.set_state(GwSshAllow.value)
    await ask_here(cb, services, state, texts.GW_SSH_ALLOW_ASK, "set_ssh")
    await cb.answer()


@router.message(GwSshAllow.value)
async def gw_ssh_allow_received(message: Message, state: FSMContext, services):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    from awgbot.infra import gwguard
    before = services._ssh_allow_split(await call(gwguard.read_env))
    try:
        after = await call(services.ssh_allow_add, raw)
    except ServiceError as e:
        await ask_tracked(message, services, f"⚠️ {texts._e(str(e))} — попробуй ещё раз")
        return
    await state.clear()
    new = [x for x in after if x not in before]
    gone = [x for x in before if x not in after]          # схлопнуто в добавленную подсеть
    await core.after_input(message, services, HOOKS, "ssh",
                           texts.gateway_ssh_allow_added(new, gone) if new or gone else texts.GW_SSH_ALLOW_ALREADY)


@router.callback_query(GwCB.filter(F.action.in_({"ssh_del", "ssh_del!", "ssh_on", "ssh_on!",
                                                  "ssh_off", "ssh_off!"})))
async def gw_ssh_action(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    """Удаление адреса, включение и выключение фильтра — сразу, без
    подтверждений: всё отменяется той же кнопкой. Включение фильтра —
    с предупреждением всплывашкой."""
    await state.clear()
    act = callback_data.action.rstrip("!")
    try:
        if act == "ssh_del":
            allow = (await call(services.ssh_screen)).get("allow") or []
            num, _dot, tag = (callback_data.val or "").partition(".")
            idx = int(num) if num.isdigit() else -1
            if not 0 <= idx < len(allow) or tag != kb.entry_tag(allow[idx]):
                await cb.answer("Список изменился — открой раздел заново", show_alert=True)
            else:
                await call(services.ssh_allow_remove, allow[idx])
                await cb.answer(f"{texts.short_name(allow[idx])} убран")
        elif act == "ssh_on":
            await call(services.ssh_filter_on)
            await cb.answer(texts.GW_SSH_FILTER_ON_ALERT, show_alert=True)
        else:
            await call(services.ssh_filter_off)
            await cb.answer(texts.GW_SSH_FILTER_OFF, show_alert=True)
    except ServiceError as e:
        await cb.answer(str(e)[:180], show_alert=True)
    await _render(cb, services, "ssh")


_CONFIRM = {                                          # текст(несёт трафик) и куда ведёт «Отмена»
    "restart": (texts.gw_confirm_restart, "settings"),
    "botrestart": (lambda carries: texts.GW_CONFIRM_BOT_RESTART, "settings"),
    "reassert": (texts.gw_confirm_reassert, "panel"),
}


@router.callback_query(GwCB.filter(F.action.in_(set(_CONFIRM))))
async def gw_confirm(cb: CallbackQuery, callback_data: GwCB, services):
    text_fn, back = _CONFIRM[callback_data.action]
    if callback_data.val in ("panel", "health"):           # откуда пришли — туда и отмена
        back = callback_data.val
    await edit_nav(cb, services, text_fn(await call(services.carries_traffic)),
                   kb.gateway_confirm_kb(callback_data.action, back))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action.in_({"restart!", "reassert!"})))
async def gw_execute(cb: CallbackQuery, callback_data: GwCB, services):
    if callback_data.action == "restart!":
        await cb.answer("Перезапускаю AWG…")
        ok, detail = await call(services.restart_link)
        title = "Перезапуск AWG"
    else:
        await cb.answer("Восстанавливаю…")
        ok, detail = await call(services.reassert)
        title = "Восстановление"
        # снимок с новым состоянием — на ВПС сейчас, а не через тик
        from awgbot.runtime import linkclient
        await linkclient.poke(services)
    # Итог остаётся в чате отдельным сообщением: «когда и чем кончилось»
    # спрашивают потом, а панель переписывается следующей навигацией.
    await edit_nav(cb, services, texts.gateway_op_result(title, ok, detail), None)
    await _panel(cb.message, services, fresh=True, keep_id=cb.message.message_id)


@router.callback_query(GwCB.filter(F.action == "botrestart!"))
async def gw_bot_restart(cb: CallbackQuery, services):
    """Как у основного: обещание на месте меню, исполняет его новый процесс
    (restore_panel_after_restart). Рестарт — вне нашего cgroup."""
    await cb.answer()
    await edit_nav(cb, services, texts.GW_BOT_RESTARTING, None)
    await call(services.set_restart_wait, cb.message.chat.id, cb.message.message_id)
    await call(services.restart_bot)


@router.callback_query(HideCB.filter())
async def gw_hide(cb: CallbackQuery):
    """«Скрыть» — последняя кнопка на любом проактивном уведомлении (алерты
    монитора, предупреждения при старте, «доступна новая версия»). У агента
    её обработчика не было: каждое нажатие уходило в «not handled», и
    уведомления было не убрать. Удаляет само сообщение, как у основного."""
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass
    await cb.answer()


# ── 🔀 VPN-транзит: один экран со своими списками ────────────────────────────

async def _own_info(services) -> dict:
    info, _checks = await call(services.own_status)
    return info


async def _transit_screen(services, chat_id: int):
    """(текст, клавиатура) экрана «🔀 VPN-транзит»: факты, списки, свои
    домены кнопками."""
    from awgbot.bot import paging
    st = await _status(services, fresh=False)
    items = await call(services.lan_own_lists)
    return (texts.gateway_transit_text(st, items, await _own_info(services)),
            kb.gateway_transit_kb(items, page=paging.page_of(chat_id, "lanlist")))


@router.callback_query(GwCB.filter(F.action.in_({"lan", "lan_list"})))
async def gw_transit(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    # сообщение под кнопкой — экран, а не служебное: приглашение к вводу
    # (core.ask) записало его в служебные, уборка снесла бы живое меню
    await call(services.db.remove_content_msg_id, cb.message.chat.id, cb.message.message_id)
    await cleanup_content(cb.message.bot, services, cb.message.chat.id)
    await edit_nav(cb, services, *await _transit_screen(services, cb.message.chat.id))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action.in_({"lan_add", "lan_ru"})))
async def gw_transit_ask(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    kind = callback_data.action.split("_", 1)[1]
    await state.set_state(GatewayTransitDomain.value)
    await state.update_data(kind=kind)
    await ask_here(cb, services, state, texts.gateway_transit_ask_domain(kind), "set_lan")
    await cb.answer()


@router.message(GatewayTransitDomain.value)
async def gw_transit_domain_received(message: Message, state: FSMContext, services):
    kind = (await state.get_data()).get("kind") or "add"
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    domains = [t for t in (message.text or "").split() if t]
    if not domains:
        await ask_tracked(message, services, "⚠️ Пришли хотя бы один домен")
        return
    await state.clear()
    ok, out = await call(services.lan_domains, kind, domains)
    await cleanup_content(message.bot, services, message.chat.id)
    # итог — первыми строками экрана, с новым доменом уже в кнопках
    from awgbot.bot import screens
    text, markup = await _transit_screen(services, message.chat.id)
    note = texts.gateway_transit_result(ok, out, await _own_sync_tail(services, ok, out),
                                    budget=texts.note_budget(text))
    await send_menu(message, services, screens.with_note(text, note), markup)


async def _own_sync_tail(services, ok: bool, out: str) -> str:
    """После правки списка: сверка и отправка серверу сразу; хвост итога — куда
    уйдёт правка, и только когда это правда: другого шлюза нет (сервер сказал)
    — хвоста нет; канала нет, а шлюзы есть — «применятся только здесь».
    Изменилось ли что-то, решает сверка файлов, а не слова вывода скрипта."""
    from awgbot.runtime import linkclient
    if not ok:
        return ""
    others = await call(services.link_standby_known)      # True / False / None — сервер не говорил
    if others is False:
        return ""
    info = await _own_info(services)
    if not info.get("active"):
        # без канала сверки с сервером нет — об изменении говорит сам скрипт
        changed = any(r.rstrip().endswith((": добавлен", ": убран")) for r in out.splitlines())
        return "no_channel" if info.get("no_channel") and changed else ""
    if not await linkclient.own_changed(services):
        return ""
    return "online" if linkclient.online() else "offline"


@router.callback_query(GwCB.filter(F.action.in_({"lan_rm", "lan_rm!"})))
async def gw_transit_remove(cb: CallbackQuery, callback_data: GwCB, services):
    """«➖ домен» — сразу, без подтверждения: всплывашка с итогом («Убран на
    всех шлюзах» при общих списках); скрипт ответил дольше, чем Telegram
    держит нажатие, — итог сообщением. Номер — по отсортированному списку;
    список успел измениться — переспрос, не чужой домен."""
    items = kb.lan_own_sorted(await call(services.lan_own_lists))
    num, _dot, tag = (callback_data.val or "").partition(".")
    idx = int(num) if num.isdigit() else -1
    if not 0 <= idx < len(items) or tag != kb.lan_own_tag(*items[idx]):   # без метки — не наша кнопка
        await cb.answer("Список изменился — открой раздел заново", show_alert=True)
        await edit_nav(cb, services, *await _transit_screen(services, cb.message.chat.id))
        return
    _kind, dom = items[idx]
    ok, out = await call(services.lan_domains, "del", [dom])
    tail = await _own_sync_tail(services, ok, out)
    try:
        if ok:
            await cb.answer(texts.gateway_transit_removed_toast(dom, sync=tail))
        else:
            import html as _html
            await cb.answer(_html.unescape(texts.gateway_transit_result(ok, out, tail))[:180], show_alert=True)
    except TelegramBadRequest:
        await ask_tracked(cb.message, services, texts.gateway_transit_result(ok, out, tail))
    await edit_nav(cb, services, *await _transit_screen(services, cb.message.chat.id))


@router.callback_query(GwCB.filter(F.action == "lan_router"))
async def gw_transit_router(cb: CallbackQuery, callback_data: GwCB, services):
    """Рецепт роутера вкладками — тот же текст, что у основного бота, с
    подсетью и настоящим адресом этого шлюза."""
    import socket
    net, addr, peers = await call(services.lan_router_params)
    tab = callback_data.val if callback_data.val in ("mt", "ow") else "mt"
    await edit_nav(cb, services,
                   texts.gateway_router_text(await call(services.link_slot_name) or socket.gethostname(),
                                             net, addr, peer_nets=peers, tab=tab),
                   kb.gateway_transit_router_kb(tab))
    await cb.answer()


async def _lan_lists_soon(services, bot, delay: float = 60.0) -> None:
    """Первые списки после включения режима без VPN бандлом. lan_lists_update
    сам промолчит, если фиды уже привёз канал."""
    from awgbot.bot.notifier import send_notifications
    await asyncio.sleep(delay)
    try:
        notes = await call(services.lan_lists_update)
        if notes:
            await send_notifications(bot, notes)
    except Exception as e:                                # noqa: BLE001
        log.warning("gateway: первые списки после бандла: %s", e)


@router.callback_query(GwCB.filter(F.action.in_({"apply!", "apply_ow!", "apply_keep!"})))
async def gw_bundle_apply(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    """apply! — первый шаг: если в файле фраза бэкапов, отличная от местной,
    сначала вопрос; apply_ow!/apply_keep! — ответ на него. Файл до решения
    остаётся в памяти диалога."""
    raw = (await state.get_data()).get("bundle")
    if not raw:
        await state.clear()
        await cb.answer("Файла в памяти нет — пришли его заново", show_alert=True)
        return
    blob = base64.b64decode(raw)
    if callback_data.action == "apply!":
        info = await call(services.inspect_bundle, blob)
        if info.get("ok") and info.get("passphrase_differs"):
            await edit_nav(cb, services, texts.GW_BUNDLE_PASSPHRASE_QUESTION,
                           kb.gateway_bundle_passphrase_kb())
            await cb.answer()
            return
    await state.clear()
    await cb.answer("Применяю…")
    ok, detail = await call(services.apply_bundle, blob, callback_data.action == "apply_ow!")
    if ok:
        # человеческий итог из статуса скрипта; хвост вывода — только при отказе
        detail = await call(services.gateway_apply_report) or detail
    await edit_nav(cb, services, texts.gateway_op_result("Конфигурация шлюза", ok, detail), None)
    # итог — серверу сразу: там у файла ждут ответа; отказ уходит тем же путём
    from awgbot.runtime import linkclient
    from awgbot.util import bundlecrypt
    await linkclient.report_applied(services, ok, "" if ok else detail, bundlecrypt.fingerprint(blob))
    if ok:
        # бандл мог только что включить канал — поднять клиента и сразу
        # отправить снимок: человек смотрит на карточку слота на ВПС сейчас
        await linkclient.poke(services)
        if await call(services.lan_lists_needed):
            # режим без VPN включён этим бандлом, списков ещё нет: не ждать
            # планового обновления. Через минуту, а не сейчас: канал, если он
            # есть, за это время привезёт фиды сам, и своё скачивание не понадобится
            asyncio.get_running_loop().create_task(_lan_lists_soon(services, cb.bot))
        # он ли помеченный шлюз: не помечен или помечен другой → токен пометки.
        # При живом канале агент отправляет его серверу сам — просить человека
        # пересылать сообщение незачем; сообщение с токеном — только когда
        # канала нет. Секунды ожидания — на подключение только что поднятого
        # клиента канала.
        outcome = await call(services.gateway_mark_outcome)
        if outcome.get("claim"):
            for _ in range(6):
                if linkclient.online():
                    break
                await asyncio.sleep(0.5)
            if linkclient.online():
                await cb.message.answer(texts.gateway_claim_via_channel_text(outcome["status"]),
                                        reply_markup=kb.hide_only())
            else:
                await cb.message.answer(texts.gateway_claim_forward_text(outcome["claim"], outcome["status"]),
                                        reply_markup=kb.hide_only())
    await _panel(cb.message, services, fresh=True, keep_id=cb.message.message_id)


@router.callback_query(GwCB.filter(F.action.in_({"restore!", "restore_drop"})))
async def gw_restore_action(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    from awgbot.bot.handlers import restore as rs
    if callback_data.action == "restore!":
        await rs.run_restore(cb, services, state)
    else:
        await rs.drop_restore(cb, state)


@router.callback_query(GwCB.filter(F.action == "drop"))
async def gw_bundle_drop(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await _panel(cb.message, services, cb)
    await cb.answer("Файл отброшен")


# ── самообновление агента (этап 3) — та же механика, что у клиентской роли ────

@router.callback_query(UpdateCB.filter(F.action == "install"))
async def gw_update_install(cb: CallbackQuery, services):
    """Скачать следующую ступень, сверить sha256, запустить апдейтер вне cgroup.
    Итог пришлёт уже новый процесс (report_update_result на старте)."""
    nxt = await call(services.update_next)
    if nxt is None:
        await cb.answer("Текущая версия актуальна", show_alert=True)
        return
    await cb.answer("Запускаю обновление…")
    chat_id = cb.message.chat.id
    await cleanup_content(cb.bot, services, chat_id)
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass
    wait = await cb.bot.send_message(chat_id, texts.update_wait(nxt.tag))
    await call(services.set_update_wait, chat_id, wait.message_id)
    try:
        await call(services.apply_update, nxt)
    except Exception as e:                            # noqa: BLE001
        await call(services.pop_update_wait)
        await call(services.db.set_state, "update_pending", "")
        try:
            await wait.delete()
        except Exception:                             # noqa: BLE001
            pass
        # отказ — финишер со «Скрыть», панель следом: меню под кнопкой уже
        # удалено, оставить человека без него нельзя
        await cb.bot.send_message(chat_id, texts.update_failed(str(e)),
                                  reply_markup=kb.hide_only())
        await _panel(cb.message, services)


@router.callback_query(UpdateCB.filter(F.action == "menu"))
async def gw_update_menu(cb: CallbackQuery, services, state: FSMContext):
    """«В меню» на итоге обновления: текст остаётся, кнопка снимается, панель —
    новым сообщением."""
    await cb.answer()
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                 # noqa: BLE001
        pass
    # и у всех прочих окон обновления тоже — живой должна быть одна кнопка
    await dismiss_update_reports(cb.bot, services,
                                 keep=(cb.message.chat.id, cb.message.message_id))
    await _panel(cb.message, services, keep_id=cb.message.message_id)


@router.callback_query(UpdateCB.filter(F.action == "mute"))
async def gw_update_mute(cb: CallbackQuery, services):
    await call(services.mute_updates)
    await cb.answer("Уведомления об обновлениях выключены")
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass


# ── раздел обновлений: ручная точка входа (уведомление могло прийти до тебя) ──

async def _updates_screen(cb: CallbackQuery, services, scan: bool = True):
    """Раздел обновлений: scan — сходить к списку релизов (при открытии);
    иначе — по тегу последней проверки, без сети."""
    await call(services.normalize_update_schedule)   # прежнее never → «месяц» и уведомления выкл
    if scan:
        found = await call(services.update_scan)
    else:
        tag = await call(services.update_available_tag)
        found = _CachedTarget(tag) if tag else None
    muted = await call(services.updates_muted)
    await edit_nav(cb, services,
                   texts.settings_upd_text(config.INSTALLED_VERSION, found, "",
                                           scan_failed=bool(getattr(services, "update_scan_failed", False))),
                   kb.gateway_updates_kb(muted, target_tag=found.tag if found is not None else ""))


class _CachedTarget:
    def __init__(self, tag: str):
        self.tag, self.body = tag, ""


@router.callback_query(GwCB.filter(F.action == "updates"))
async def gw_updates_screen(cb: CallbackQuery, services):
    await cb.answer("Проверяю…")
    await _updates_screen(cb, services, scan=True)


@router.callback_query(GwCB.filter(F.action == "upd_toggle"))
async def gw_updates_toggle(cb: CallbackQuery, services):
    muted = await call(services.updates_muted)
    if muted:
        await call(services.unmute_updates)
    else:
        await call(services.mute_updates)
    await cb.answer("Уведомления " + ("включены" if muted else "выключены"))
    await _updates_screen(cb, services, scan=False)


@router.callback_query(GwCB.filter(F.action == "upd_sched"))
async def gw_updates_sched(cb: CallbackQuery, callback_data: GwCB, services):
    """Старый пикер расписания из сообщений 3.1.0: пишется горячо; «никогда»
    больше нет — становится «месяц»."""
    from awgbot.core import settings
    opt = callback_data.val if callback_data.val in ("day", "week", "month") else "month"
    try:
        await call(settings.set_value, "updates.poll_schedule", opt)
    except settings.SettingsWriteError as e:
        await cb.answer(str(e), show_alert=True)
        return
    if callback_data.val == "never":                       # «никогда» 3.1.0 = тишина
        await call(services.mute_updates)
    await cb.answer(texts.cycle_toast("updates.poll_schedule", opt))
    await _updates_screen(cb, services, scan=False)


@router.callback_query(GwCB.filter(F.action == "upd_check"))
async def gw_updates_check(cb: CallbackQuery, services):
    """Старая кнопка «Проверить сейчас»: раздел проверяет сам при открытии."""
    await cb.answer("Проверяю…")
    await _updates_screen(cb, services, scan=True)
