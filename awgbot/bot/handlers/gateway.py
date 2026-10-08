"""
handlers/gateway.py — панель и операции агента шлюза (роль gateway, только админ).

Панель, «🩺 Здоровье», «🔧 Восстановить» (реассерт через подтверждение),
экран «🔀 VPN-транзит» со своими списками и рецептом роутера, «🛡 SSH-доступ»,
приём шифрованного бандла файлом. Общие разделы «⚙️ Настроек» (уведомления,
почта, мониторинг, бэкапы, сервис с перезапусками, обновления) — в
bot/sections со словарём GATEWAY; отсюда им — только экраны ролевых разделов
(_section: «ssh», «lan», прочее — панель) через br.role_screens. Пробы и
операции блокирующие (subprocess) — через call, как всё синхронное в проекте.
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
from awgbot.bot import texts, ui
from awgbot.bot.roles import GATEWAY
from awgbot.bot.callbacks import CancelCB, GwCB
from awgbot.bot.states import GatewayTransitDomain
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers import settingscore as core

from awgbot.bot.handlers.common import (call, edit_nav, send_menu, send_menu_to, cleanup_content, purge_menus,
                                        ask_here, forget_secret, ask_tracked,
                                        drop_previous_nav)
from awgbot.bot.states import SshPort, GwSshAllow
from awgbot.domain.services import ServiceError
from awgbot.util import bundlecrypt
from awgbot.runtime import linkclient

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
        await edit_nav(cb, services, texts.gateway_panel(st, tag, linkclient.view()), kb.gateway_panel_kb(lan))
    else:
        await send_menu(target, services, texts.gateway_panel(st, tag, linkclient.view()), kb.gateway_panel_kb(lan),
                        keep_id=keep_id)


async def _panel_parts(services, *, fresh: bool = False):
    """Текст и клавиатура панели агента — для показа без входящего сообщения."""
    st = await _status(services, fresh=fresh)
    return (texts.gateway_panel(st, await call(services.update_available_tag), linkclient.view()),
            kb.gateway_panel_kb(bool(getattr(st, "lan", None))))


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
    await send_menu_to(bot, services, chat_id, *await _panel_parts(services))


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
        await send_menu_to(bot, services, config.ADMIN_ID, *await _panel_parts(services))
    except Exception:                                  # noqa: BLE001
        return                                         # диалога ещё нет
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


# ── ролевые разделы агента: SSH-доступ и VPN-транзит; общие — bot/sections ──

def _ssh_section(services):
    from awgbot.bot import paging
    st = services.ssh_screen()
    return texts.gateway_ssh_text(st), kb.gateway_ssh_kb(st, page=paging.page_of(config.ADMIN_ID, "gwssh"))


_SECTIONS = {"ssh": _ssh_section}


async def _section(sec: str, services, key: str = ""):
    """Экран ролевого раздела агента — для sections.screen (br.role_screens)
    и реестра экранов: «ssh» и «lan»; прочее — панель."""
    if sec == "lan":
        return await _transit_screen(services, config.ADMIN_ID)
    if sec in _SECTIONS:
        return await call(_SECTIONS[sec], services)
    return await _panel_parts(services)


async def _render(cb: CallbackQuery, services, sec: str) -> None:
    await edit_nav(cb, services, *await _section(sec, services))


async def _shared(cb: CallbackQuery, callback_data: GwCB, services, state=None) -> bool:
    """Колбэк общего раздела — диспетчеру sections (из тестов, которые зовут
    прежние обработчики напрямую); True — обработан."""
    from awgbot.bot import sections as secs
    packed = secs.resolve(GATEWAY, callback_data.pack())
    if packed is None:
        return False
    await secs.handle(cb, packed, services, state, GATEWAY)
    return True


async def gw_settings(cb: CallbackQuery, services, state: FSMContext):
    await _shared(cb, GwCB(action="settings"), services, state)


async def gw_section(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    if not await _shared(cb, callback_data, services, state):
        await gw_ssh_section(cb, services, state)


async def gw_toggle(cb: CallbackQuery, callback_data: GwCB, services):
    await _shared(cb, callback_data, services)


async def gw_edit(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    await _shared(cb, callback_data, services, state)


async def gw_cycle(cb: CallbackQuery, callback_data: GwCB, services):
    await _shared(cb, callback_data, services)


async def gw_encryption(cb: CallbackQuery, services, state: FSMContext):
    await _shared(cb, GwCB(action="enc"), services, state)


async def gw_encryption_set(cb: CallbackQuery, services, state: FSMContext):
    await _shared(cb, GwCB(action="enc_set"), services, state)


async def gw_backup_now(cb: CallbackQuery, services):
    await _shared(cb, GwCB(action="backup!"), services)


async def gw_email_action(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    await _shared(cb, callback_data, services, state)


async def gw_restore_action(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    await _shared(cb, callback_data, services, state)


async def gw_bot_restart(cb: CallbackQuery, services):
    await _shared(cb, GwCB(action="botrestart!"), services)


async def gw_updates_screen(cb: CallbackQuery, services):
    await _shared(cb, GwCB(action="updates"), services)


async def gw_updates_toggle(cb: CallbackQuery, services):
    await _shared(cb, GwCB(action="upd_toggle"), services)


async def gw_update_install(cb: CallbackQuery, services):
    from awgbot.bot.sections import updates as _upd
    await _upd.install(cb, services)


async def gw_update_menu(cb: CallbackQuery, services, state: FSMContext):
    from awgbot.bot.sections import updates as _upd
    await _upd.menu(cb, services, state)


async def gw_update_mute(cb: CallbackQuery, services):
    from awgbot.bot.sections import updates as _upd
    await _upd.mute(cb, services)


async def gw_cancel_inline(cb: CallbackQuery, callback_data: CancelCB, services, state: FSMContext):
    from awgbot.bot.handlers.reply_commands import on_cancel_inline
    await on_cancel_inline(cb, callback_data, state, services, role="admin")


@router.callback_query(GwCB.filter(F.action == "ssh"))
async def gw_ssh_section(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await _render(cb, services, "ssh")
    await cb.answer()


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


# Ввод значения, парольная фраза и мастер почты регистрирует роутер общих
# разделов; те же функции по именам — для тестов.
_core = core.register(Router(name="gateway.dialogs"), __import__("awgbot.bot.sections", fromlist=["hooks_for"]).hooks_for(GATEWAY),
                      default_sec="mon")
gw_receive_value, gw_passphrase_first, gw_passphrase_second = (
    _core["receive_value"], _core["passphrase_first"], _core["passphrase_second"])


def _hooks():
    from awgbot.bot import sections as secs
    return secs.hooks_for(GATEWAY)


# ── 🛡 Доступ по SSH ─────────────────────────────────────────────────────────
# Та же механика, что в разделе основного бота (handlers/settings/inputs.py): ввод
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
                       texts.ssh_owner_refusal(st, None if st.get("sshd_down") else st["port"], GATEWAY),
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
        await send_menu(cb.message, services, texts.ssh_port_ask(st.get("port"), GATEWAY), kb.cancel_input("set_ssh"),
                        keep_id=cb.message.message_id)         # финишер остаётся с одной «Скрыть»
    else:
        await ask_here(cb, services, state, texts.ssh_port_ask(st.get("port"), GATEWAY), "set_ssh")
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "ssh_port_back"))
async def gw_ssh_port_back(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.hide_only())
    except Exception:                                     # noqa: BLE001
        pass
    await send_menu(cb.message, services, *await _section("ssh", services), keep_id=cb.message.message_id)
    await cb.answer()


def _port_dialog(services) -> core.PortDialog:
    return core.PortDialog(
        screen=services.ssh_screen, port_key="port", sec="ssh",
        owner_refusal=lambda st, listening: (texts.ssh_owner_refusal(st, listening, GATEWAY),
                                             kb.gateway_back_kb("ssh")),
        finisher_kb=kb.gateway_ssh_port_finisher_kb, changed_text=texts.gateway_ssh_port_changed)


@router.message(SshPort.value)
async def gw_ssh_port_received(message: Message, state: FSMContext, services):
    await core.port_received(message, state, services, _hooks(), _port_dialog(services))


@router.callback_query(GwCB.filter(F.action == "ssh_add"))
async def gw_ssh_allow_ask(cb: CallbackQuery, services, state: FSMContext):
    await state.set_state(GwSshAllow.value)
    await ask_here(cb, services, state, texts.GW_SSH_ALLOW_ASK, "set_ssh")
    await cb.answer()


@router.message(GwSshAllow.value)
async def gw_ssh_allow_received(message: Message, state: FSMContext, services):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    before = await call(services.ssh_allow_current)
    try:
        after = await call(services.ssh_allow_add, raw)
    except ServiceError as e:
        await ask_tracked(message, services, f"⚠️ {texts._e(str(e))} — попробуй ещё раз")
        return
    await state.clear()
    new = [x for x in after if x not in before]
    gone = [x for x in before if x not in after]          # схлопнуто в добавленную подсеть
    await core.after_input(message, services, _hooks(), "ssh",
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


# Перезапуски AWG и бота — «🔧 Сервис» общих разделов; здесь — восстановление
# шлюза с панели и экрана здоровья.
@router.callback_query(GwCB.filter(F.action == "reassert"))
async def gw_confirm(cb: CallbackQuery, callback_data: GwCB, services):
    if callback_data.action != "reassert":
        await _shared(cb, callback_data, services)
        return
    back = callback_data.val if callback_data.val in ("panel", "health") else "panel"   # откуда пришли — туда и отмена
    await edit_nav(cb, services, texts.gw_confirm_reassert(await call(services.carries_traffic)),
                   kb.gateway_confirm_kb("reassert", back))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "reassert!"))
async def gw_execute(cb: CallbackQuery, callback_data: GwCB, services):
    if callback_data.action != "reassert!":
        await _shared(cb, callback_data, services)
        return
    await cb.answer("Восстанавливаю…")
    ok, detail = await call(services.reassert)
    # снимок с новым состоянием — на ВПС сейчас, а не через тик
    from awgbot.runtime import linkclient
    await linkclient.poke(services)
    # Итог остаётся в чате отдельным сообщением: «когда и чем кончилось»
    # спрашивают потом, а панель переписывается следующей навигацией.
    await edit_nav(cb, services, texts.gateway_op_result("Восстановление", ok, detail), None)
    await _panel(cb.message, services, fresh=True, keep_id=cb.message.message_id)


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
    await cleanup_content(cb.message.bot, services, cb.message.chat.id, keep=cb.message.message_id)
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
                                    budget=ui.note_budget(text))
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
    mail = await call(services.bundle_mail_check)
    await edit_nav(cb, services, texts.gateway_config_result(ok, detail, mail), None)
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


@router.callback_query(GwCB.filter(F.action == "drop"))
async def gw_bundle_drop(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await _panel(cb.message, services, cb)
    await cb.answer("Файл отброшен")
