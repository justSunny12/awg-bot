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
from awgbot.bot.filters import RoleFilter
from awgbot.bot.states import SettingsInput
from awgbot.bot.handlers.common import call, edit_nav, send_menu, cleanup_content, purge_menus, dismiss_update_reports
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


async def _panel(target, services, cb: CallbackQuery | None = None, fresh: bool = False):
    """Панель — через нав-хелперы: одно живое меню в чате, прошлое гаснет,
    история ведётся для /start."""
    st = await _status(services, fresh)
    if cb is not None:
        await edit_nav(cb, services, texts.gateway_panel(st), kb.gateway_panel_kb())
    else:
        await send_menu(target, services, texts.gateway_panel(st), kb.gateway_panel_kb())


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
                                  reply_markup=kb.gateway_panel_kb())
    await call(services.db.set_nav_message_id, chat_id, sent.message_id)
    await call(services.db.push_nav_history, chat_id, sent.message_id)


@router.message(CommandStart())
async def gw_start(message: Message, services, state: FSMContext):
    await state.clear()
    await purge_menus(message.bot, services, message.chat.id)
    await _panel(message, services)


@router.callback_query(GwCB.filter(F.action == "panel"))
async def gw_panel(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await _panel(cb.message, services, cb)
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "refresh"))
async def gw_refresh(cb: CallbackQuery, services, state: FSMContext):
    """«Обновить» — единственная кнопка, которая всегда ходит по пробам живьём."""
    await state.clear()
    await cb.answer("Снимаю показания…")
    await _panel(cb.message, services, cb, fresh=True)


@router.callback_query(GwCB.filter(F.action == "health"))
async def gw_health(cb: CallbackQuery, services):
    await cb.answer("Проверяю…")
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

_SECTIONS = {
    "notify": lambda: (texts.GW_SETTINGS_NOTIFY, kb.gateway_notify_kb()),
    "mon": lambda: (texts.GW_SETTINGS_MON, kb.gateway_mon_kb()),
    "backup": lambda: (texts.SETTINGS_BACKUP, kb.gateway_backup_kb()),
}


@router.callback_query(GwCB.filter(F.action.in_(set(_SECTIONS))))
async def gw_section(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    await state.clear()
    await edit_nav(cb, services, *_SECTIONS[callback_data.action]())
    await cb.answer()


def _section_of(key: str) -> str:
    if key.startswith("quiet_hours.") or key.startswith("resource_alerts.") or key == "app.gateway.temp_alert_c":
        return "notify"
    if key.startswith("app.scheduler.backup_"):
        return "backup"
    return "mon"


@router.callback_query(GwCB.filter(F.action == "tgl"))
async def gw_toggle(cb: CallbackQuery, callback_data: GwCB, services):
    """Тумблер bool в conf — та же механика, что у основного бота."""
    from awgbot.core import settings
    key = callback_data.val
    cur = settings.get_bool(key, True)
    try:
        await call(settings.set_value, key, not cur)
    except settings.SettingsWriteError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await edit_nav(cb, services, *_SECTIONS[_section_of(key)]())
    await cb.answer()


@router.callback_query(GwCB.filter(F.action == "edit"))
async def gw_edit(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    key = callback_data.val
    if key not in texts.SETTINGS_BOUNDS:
        await cb.answer("Эта настройка недоступна.", show_alert=True)
        return
    sec = _section_of(key)
    await state.set_state(SettingsInput.value)
    await state.update_data(key=key, sec=sec)
    await edit_nav(cb, services, texts.settings_prompt(key), kb.gateway_cancel_kb(sec))
    await cb.answer()


@router.message(SettingsInput.value)
async def gw_receive_value(message: Message, state: FSMContext, services):
    from awgbot.core import settings
    data = await state.get_data()
    key, sec = data.get("key"), data.get("sec", "mon")
    if key not in texts.SETTINGS_BOUNDS:
        await state.clear()
        await send_menu(message, services, *_SECTIONS[sec]())
        return
    lo, hi, _label, _unit = texts.SETTINGS_BOUNDS[key]
    try:
        val = int((message.text or "").strip())
        if not (lo <= val <= hi):
            raise ValueError
    except ValueError:
        await message.answer(texts.settings_bad_value(key))
        return
    try:
        await call(settings.set_value, key, val)
    except settings.SettingsWriteError as e:
        await state.clear()
        await message.answer(str(e))
        return
    await state.clear()
    await send_menu(message, services, *_SECTIONS[sec]())


@router.callback_query(GwCB.filter(F.action == "backup!"))
async def gw_backup_now(cb: CallbackQuery, services):
    from aiogram.types import FSInputFile
    await cb.answer("Готовлю резервную копию…")
    try:
        paths = await call(services.make_backup)
    except Exception as e:                            # noqa: BLE001
        await cb.message.answer(texts.GW_BACKUP_NO_KEY if "шифрован" in str(e) else f"⚠️ {e}")
        return
    for p in paths:
        try:
            await cb.message.answer_document(FSInputFile(p))
        except Exception:                             # noqa: BLE001
            pass
    await edit_nav(cb, services, *_SECTIONS["backup"]())


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
    await _panel(cb.message, services, fresh=True)


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


# ── бандл файлом ─────────────────────────────────────────────────────────────

@router.message(F.document)
async def gw_bundle_document(message: Message, services, state: FSMContext):
    doc = message.document
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
    await state.update_data(bundle=base64.b64encode(blob).decode())
    await message.answer(texts.GW_BUNDLE_RECEIVED, reply_markup=kb.gateway_bundle_kb())


@router.callback_query(GwCB.filter(F.action == "apply!"))
async def gw_bundle_apply(cb: CallbackQuery, services, state: FSMContext):
    raw = (await state.get_data()).get("bundle")
    await state.clear()
    if not raw:
        await cb.answer("Файла в памяти нет — пришли его заново.", show_alert=True)
        return
    await cb.answer("Применяю…")
    ok, detail = await call(services.apply_bundle, base64.b64decode(raw))
    await edit_nav(cb, services, texts.gateway_op_result("Конфигурация шлюза", ok, detail), None)
    await _panel(cb.message, services, fresh=True)


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
        failed_msg = await cb.bot.send_message(chat_id, texts.update_failed(str(e)),
                                               reply_markup=kb.update_done_menu())
        await call(services.remember_update_report, chat_id, failed_msg.message_id)


@router.callback_query(UpdateCB.filter(F.action == "menu"))
async def gw_update_menu(cb: CallbackQuery, services, state: FSMContext):
    """«В меню» на итоге обновления: текст остаётся, кнопка снимается, панель —
    новым сообщением."""
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                 # noqa: BLE001
        pass
    # и у всех прочих окон обновления тоже — живой должна быть одна кнопка
    await dismiss_update_reports(cb.bot, services)
    await _panel(cb.message, services)
    await cb.answer()


@router.callback_query(UpdateCB.filter(F.action == "mute"))
async def gw_update_mute(cb: CallbackQuery, services):
    await call(services.mute_updates)
    await cb.answer("Уведомления об обновлениях выключены.")
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass


# ── раздел обновлений: ручная точка входа (уведомление могло прийти до тебя) ──

_SCHED_RU = {"day": "каждый день", "week": "раз в неделю", "month": "раз в месяц",
             "never": "никогда"}


async def _updates_screen(cb: CallbackQuery, services):
    from awgbot.core import config, settings
    muted = await call(services.updates_muted)
    sched = _SCHED_RU.get(str(settings.get("updates.poll_schedule", "day")).lower(), "?")
    await edit_nav(cb, services,
                   texts.gateway_updates(config.INSTALLED_VERSION, muted, sched),
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
                   texts.update_admin_available(config.INSTALLED_VERSION, nxt.tag, nxt.body),
                   kb.gateway_update_available_kb())
