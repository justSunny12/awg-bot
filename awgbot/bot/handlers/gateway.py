"""
handlers/gateway.py — панель и операции агента шлюза (роль gateway, только админ).

Этап 1 — панель; этап 2 — доктор, рестарт линка и реассерт (через
подтверждение), приём шифрованного бандла файлом. Пробы и операции блокирующие
(subprocess) — через call, как всё синхронное в проекте.
"""
from __future__ import annotations

import base64
import io

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB, HideCB, UpdateCB
from awgbot.bot.states import GatewayLanDomain
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers import settingscore as core
from awgbot.bot.handlers.common import (call, edit_nav, send_menu, cleanup_content, purge_menus,
                                        dismiss_update_reports, forget_secret, ask_tracked,
                                        drop_previous_nav)
from awgbot.bot.states import SshPort, GwSshAllow
from awgbot.domain.gwssh import SshOwnerRefusal
from awgbot.domain.services import ServiceError
from awgbot.util import bundlecrypt

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
    if cb is not None:
        await edit_nav(cb, services, texts.gateway_panel(st), kb.gateway_panel_kb(lan))
    else:
        await send_menu(target, services, texts.gateway_panel(st), kb.gateway_panel_kb(lan),
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
    sent = await bot.send_message(chat_id, texts.gateway_panel(st),
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
        sent = await bot.send_message(config.ADMIN_ID, texts.gateway_panel(st),
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
    st = await call(services.status)
    await edit_nav(cb, services, texts.gateway_health(st), kb.gateway_back_kb())


@router.callback_query(GwCB.filter(F.action == "settings"))
async def gw_settings(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await edit_nav(cb, services, texts.GW_SETTINGS, kb.gateway_settings_kb())
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "maint"))
async def gw_maint(cb: CallbackQuery, services):
    await edit_nav(cb, services, texts.GW_MAINT, kb.gateway_maint_kb())
    await cb.answer()


# ── разделы настроек: уведомления / мониторинг / резервное копирование ───────

def _email_section(services):
    acc = services.email_account()
    return (texts.settings_email_text(acc, services.email_last_check()),
            kb.gateway_email_kb(acc is not None))


def _ssh_section(services):
    st = services.ssh_screen()
    return texts.gateway_ssh_text(st), kb.gateway_ssh_kb(st)


_SECTIONS = {
    "notify": lambda services: (texts.GW_SETTINGS_NOTIFY, kb.gateway_notify_kb()),
    "ssh": _ssh_section,
    "email": _email_section,
    "mon": lambda services: (texts.GW_SETTINGS_MON, kb.gateway_mon_kb()),
    "backup": lambda services: (texts.SETTINGS_BACKUP,
                                kb.gateway_backup_kb(services.backup_encryption_enabled())),
}


async def _section(services, sec: str):
    return await call(_SECTIONS[sec], services)


async def _render(cb: CallbackQuery, services, sec: str) -> None:
    await edit_nav(cb, services, *await _section(services, sec))


# Общая механика диалогов настроек — в settingscore; здесь только колбэки и
# клавиатуры агента.
HOOKS = core.Hooks(
    cancel_kb=kb.gateway_cancel_kb,
    email_offer_kb=kb.gateway_email_offer,
    email_forget_kb=kb.gateway_email_forget_confirm,
    render=_render,
    screen=_section,
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
    await edit_nav(cb, services, texts.backup_encryption_text(mode), kb.gateway_encryption_kb(bool(mode)))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "enc_set"))
async def gw_encryption_set(cb: CallbackQuery, services, state: FSMContext):
    await core.passphrase_start(cb, services, HOOKS, state)


def _section_of(key: str) -> str:
    if (key.startswith("quiet_hours.") or key.startswith("resource_alerts.")
            or key.startswith("notifications.") or key == "app.gateway.temp_alert_c"):
        return "notify"
    if key.startswith("app.scheduler.backup_"):
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


# ── ✉️ E-mail у агента: ящик из бандла или руками, проверка, отключение ─────

_EMAIL_KEYS = {"em_setup": "setup", "em_check": "check", "em_test": "test",
               "em_forget": "forget", "em_forget!": "forget!"}


@router.callback_query(GwCB.filter(F.action.in_(set(_EMAIL_KEYS))))
async def gw_email_action(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    await core.email_action(cb, services, HOOKS, state, _EMAIL_KEYS[callback_data.action])


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
        await send_menu(cb.message, services, texts.GW_SSH_PORT_ASK, kb.gateway_cancel_kb("ssh"))
    else:
        await core.ask(cb, services, texts.GW_SSH_PORT_ASK, kb.gateway_cancel_kb("ssh"))
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
        await ask_tracked(message, services, "⚠️ Порт — число от 1 до 65535. Попробуй ещё раз.")
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
        await message.answer(f"⚠️ Порт не изменён: {texts._e(str(e))}")
    else:
        await message.answer(texts.gateway_ssh_port_changed(old, port), reply_markup=kb.hide_only())
    await core.after_input(message, services, HOOKS, "ssh")


@router.callback_query(GwCB.filter(F.action == "ssh_add"))
async def gw_ssh_allow_ask(cb: CallbackQuery, services, state: FSMContext):
    await state.set_state(GwSshAllow.value)
    await core.ask(cb, services, texts.GW_SSH_ALLOW_ASK, kb.gateway_cancel_kb("ssh"))
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
        await ask_tracked(message, services, f"⚠️ {texts._e(str(e))}. Попробуй ещё раз.")
        return
    await state.clear()
    new = [x for x in after if x not in before]
    gone = [x for x in before if x not in after]          # схлопнуто в добавленную подсеть
    await message.answer(texts.gateway_ssh_allow_added(new, gone) if new or gone
                         else texts.GW_SSH_ALLOW_ALREADY)
    await core.after_input(message, services, HOOKS, "ssh")


@router.callback_query(GwCB.filter(F.action.in_({"ssh_del", "ssh_del!", "ssh_on", "ssh_on!",
                                                  "ssh_off", "ssh_off!"})))
async def gw_ssh_action(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    """Удаление адреса, включение и выключение фильтра — с подтверждения
    («Отмена» первой): всё это меняет, кто попадёт на шлюз снаружи."""
    await state.clear()
    act = callback_data.action
    try:
        if act == "ssh_del":
            allow = (await call(services.ssh_screen)).get("allow") or []
            idx = int(callback_data.val) if callback_data.val.isdigit() else -1
            if not 0 <= idx < len(allow):
                await cb.answer("Список изменился — открой раздел заново", show_alert=True)
            else:
                await edit_nav(cb, services, texts.gateway_ssh_del_ask(allow[idx]),
                               kb.gateway_ssh_confirm_kb("ssh_del!", str(idx), "➖ Убрать"))
                await cb.answer()
                return
        elif act == "ssh_del!":
            allow = (await call(services.ssh_screen)).get("allow") or []
            idx = int(callback_data.val) if callback_data.val.isdigit() else -1
            if not 0 <= idx < len(allow):
                await cb.answer("Список изменился — открой раздел заново", show_alert=True)
            else:
                await call(services.ssh_allow_remove, allow[idx])
                await cb.answer(f"{allow[idx]} убран")
        elif act == "ssh_on":
            st = await call(services.ssh_screen)
            await edit_nav(cb, services, texts.gateway_ssh_filter_on_ask(st["port"]),
                           kb.gateway_ssh_confirm_kb("ssh_on!", "", "🟢 Включить"))
            await cb.answer()
            return
        elif act == "ssh_on!":
            await call(services.ssh_filter_on)
            await cb.answer("Фильтр включён")
        elif act == "ssh_off":
            await edit_nav(cb, services, texts.GW_SSH_FILTER_OFF_ASK,
                           kb.gateway_ssh_confirm_kb("ssh_off!", "", "🔴 Выключить"))
            await cb.answer()
            return
        else:
            await call(services.ssh_filter_off)
            await cb.answer(texts.GW_SSH_FILTER_OFF, show_alert=True)
    except ServiceError as e:
        await cb.answer(str(e)[:180], show_alert=True)
    await _render(cb, services, "ssh")


_CONFIRM = {
    "restart": (lambda: texts.GW_CONFIRM_RESTART, "maint"),
    "botrestart": (lambda: texts.GW_CONFIRM_BOT_RESTART, "maint"),
    "reassert": (lambda: texts.GW_CONFIRM_REASSERT, "panel"),
}


@router.callback_query(GwCB.filter(F.action.in_(set(_CONFIRM))))
async def gw_confirm(cb: CallbackQuery, callback_data: GwCB, services):
    text_fn, back = _CONFIRM[callback_data.action]
    await edit_nav(cb, services, text_fn(), kb.gateway_confirm_kb(callback_data.action, back))
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
        title = "Мастер восстановления"
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


# ── локальная сеть без VPN (концепт «локальная сеть» §3.5) ────────────────────────

async def _lan_screen(services):
    st = await _status(services, fresh=False)
    return texts.gateway_lan_text(st), kb.gateway_lan_kb()


@router.callback_query(GwCB.filter(F.action == "lan"))
async def gw_lan(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await cleanup_content(cb.message.bot, services, cb.message.chat.id)
    await edit_nav(cb, services, *await _lan_screen(services))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action.in_({"lan_add", "lan_ru", "lan_del"})))
async def gw_lan_ask(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    kind = callback_data.action.split("_", 1)[1]
    await state.set_state(GatewayLanDomain.value)
    await state.update_data(kind=kind)
    await core.ask(cb, services, texts.gateway_lan_ask_domain(kind), kb.gateway_cancel_kb("lan"))
    await cb.answer()


@router.message(GatewayLanDomain.value)
async def gw_lan_domain_received(message: Message, state: FSMContext, services):
    kind = (await state.get_data()).get("kind") or "add"
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    domains = [t for t in (message.text or "").split() if t]
    if not domains:
        await ask_tracked(message, services, "⚠️ Пришли хотя бы один домен.")
        return
    await state.clear()
    ok, out = await call(services.lan_domains, kind, domains)
    await cleanup_content(message.bot, services, message.chat.id)
    await message.answer(texts.gateway_lan_result(ok, out))
    await send_menu(message, services, *await _lan_screen(services))


@router.callback_query(GwCB.filter(F.action == "lan_list"))
async def gw_lan_list(cb: CallbackQuery, services):
    items = await call(services.lan_own_lists)
    await edit_nav(cb, services, texts.gateway_lan_own_text(items), kb.gateway_lan_list_kb())
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "lan_update"))
async def gw_lan_update(cb: CallbackQuery, services):
    """Фиды сейчас: секунды, поэтому синхронно с отбивкой «обновляю»."""
    await cb.answer("Обновляю списки…")
    ok, tail = await call(services.lan_lists_now)
    await cb.message.answer(texts.gateway_lan_result(ok, tail or ("списки обновлены" if ok else "")))
    # назад — туда, откуда нажали: на экран локальной сети, со свежими цифрами
    await send_menu(cb.message, services, *await _lan_screen(services))


@router.callback_query(GwCB.filter(F.action == "lan_router"))
async def gw_lan_router(cb: CallbackQuery, services):
    """Рецепт роутера — тот же текст, что у основного бота, с подсетью и
    адресом этого шлюза (их знает только он)."""
    import socket
    net, addr = await call(services.lan_router_params)
    # «Назад» — на экран локальной сети, откуда пришли: с клавиатурой того экрана
    # рецепт читался бы как сам экран «Локальная сеть»
    await edit_nav(cb, services, texts.gateway_router_text(socket.gethostname(), net, addr),
                   kb.gateway_back_kb("lan"))
    await cb.answer()


# ── бандл файлом ─────────────────────────────────────────────────────────────

@router.message(F.document)
async def gw_bundle_document(message: Message, services, state: FSMContext):
    doc = message.document
    from awgbot.bot.handlers import restore as rs
    if rs.looks_like_backup(doc):                     # резервная копия, не конфигурация
        await rs.offer_restore(message, services, state, gateway=True)
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
    await send_menu(message, services, texts.gateway_bundle_received(link_changed),
                    kb.gateway_bundle_kb())


@router.callback_query(GwCB.filter(F.action.in_({"apply!", "apply_ow!", "apply_keep!"})))
async def gw_bundle_apply(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    """apply! — первый шаг: если в файле фраза бэкапов, отличная от местной,
    сначала вопрос; apply_ow!/apply_keep! — ответ на него. Файл до решения
    остаётся в памяти диалога."""
    raw = (await state.get_data()).get("bundle")
    if not raw:
        await state.clear()
        await cb.answer("Файла в памяти нет — пришли его заново.", show_alert=True)
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
    if ok:
        # он ли помеченный шлюз: не помечен или помечен другой → сообщение для
        # пересылки основному боту отдельным сообщением, чтобы пересылалось как есть
        outcome = await call(services.gateway_mark_outcome)
        if outcome.get("claim"):
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
        await cb.answer("Обновлять не на что — версия актуальна.", show_alert=True)
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
    await cb.answer("Уведомления об обновлениях выключены.")
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass


# ── раздел обновлений: ручная точка входа (уведомление могло прийти до тебя) ──

async def _updates_screen(cb: CallbackQuery, services):
    from awgbot.core import config
    muted = await call(services.updates_muted)
    await edit_nav(cb, services, texts.settings_upd_text(config.INSTALLED_VERSION),
                   kb.gateway_updates_kb(muted))


@router.callback_query(GwCB.filter(F.action == "updates"))
async def gw_updates_screen(cb: CallbackQuery, services):
    await _updates_screen(cb, services)
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "upd_toggle"))
async def gw_updates_toggle(cb: CallbackQuery, services):
    from awgbot.core import settings
    # как у основного: при расписании «никогда» включить уведомления нельзя —
    # проверять нечему, и тумблер «вкл» обещал бы то, чего не будет
    if str(settings.get("updates.poll_schedule", "day")).lower() == "never":
        await cb.answer("Сначала выбери расписание проверки (не «никогда»).",
                        show_alert=True)
        return
    if await call(services.updates_muted):
        await call(services.unmute_updates)
    else:
        await call(services.mute_updates)
    await _updates_screen(cb, services)
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "upd_sched"))
async def gw_updates_sched(cb: CallbackQuery, callback_data: GwCB, services):
    """Пикер расписания: пишется в conf горячо, задача перевешивается хуком
    settings.on_change в скедулере агента; «никогда» — авто-мьют уведомлений."""
    from awgbot.core import settings
    opt = callback_data.val
    if opt not in ("day", "week", "month", "never"):
        await cb.answer("Нет такого варианта.", show_alert=True)
        return
    try:
        await call(settings.set_value, "updates.poll_schedule", opt)
    except settings.SettingsWriteError as e:
        await cb.answer(str(e), show_alert=True)
        return
    if opt == "never":
        await call(services.mute_updates)
    await _updates_screen(cb, services)
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "upd_check"))
async def gw_updates_check(cb: CallbackQuery, services):
    """Проверить сейчас: есть ступень — показать с кнопкой «Обновить» (тот же
    UpdateCB, что и в уведомлении); нет — сказать, что версия актуальна."""
    from awgbot.core import config
    await cb.answer("Проверяю…")
    nxt = await call(services.update_next)
    if nxt is None:
        await edit_nav(cb, services, texts.update_current_ok(config.INSTALLED_VERSION),
                       kb.gateway_updates_kb(await call(services.updates_muted)))
        return
    await edit_nav(cb, services,
                   texts.update_admin_available(config.INSTALLED_VERSION, nxt.tag, nxt.body, nxt.skipped),
                   kb.gateway_update_available_kb())
