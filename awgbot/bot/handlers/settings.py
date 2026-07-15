"""handlers/settings.py — экран «⚙️ Настройки» (только админ).

Значения хранятся в conf/*.yaml и меняются через settings.set_value → горячо,
без рестарта. Экран перерисовывается после каждого изменения и показывает
актуальные значения. Раздел под RoleFilter("admin"), как остальная админка.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, FSInputFile

from awgbot.core import config
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import SetCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.states import SettingsInput
from awgbot.bot.handlers.common import call, edit

router = Router(name="settings")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))


# ── рендер экранов ───────────────────────────────────────────────────────────
def _screen(sec: str, services):
    """(text, markup) для раздела sec."""
    if sec == "notify":
        return texts.SETTINGS_NOTIFY, kb.settings_notify()
    if sec == "subs":
        return texts.SETTINGS_SUBS, kb.settings_subs()
    if sec == "mon":
        return texts.SETTINGS_MON, kb.settings_mon()
    if sec == "backup":
        return texts.SETTINGS_BACKUP, kb.settings_backup()
    if sec == "svc":
        return texts.SETTINGS_SVC, kb.settings_svc()
    if sec == "upd":
        return texts.SETTINGS_UPD, kb.settings_updates(services.updates_muted())
    return texts.SETTINGS_ROOT, kb.settings_root()


async def _render(cb: CallbackQuery, sec: str, services):
    text, markup = _screen(sec, services)
    await edit(cb, text, markup)


# ── открытие раздела ─────────────────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "open"))
async def open_section(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    await state.clear()
    await _render(cb, callback_data.sec, services)
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
        muted = services.updates_muted()
        if muted:
            await call(services.unmute_updates)
        else:
            await call(services.mute_updates)
    else:
        cur = settings.get_bool(key, True)
        await call(settings.set_value, key, not cur)
    await _render(cb, callback_data.sec, services)
    await cb.answer()


# ── ввод числового значения (FSM) ────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "edit"))
async def edit_value(cb: CallbackQuery, callback_data: SetCB, state: FSMContext):
    key = callback_data.key
    await state.set_state(SettingsInput.value)
    await state.update_data(key=key, sec=callback_data.sec)
    await edit(cb, texts.settings_prompt(key), kb.settings_cancel(callback_data.sec))
    await cb.answer()


@router.message(SettingsInput.value)
async def receive_value(message: Message, state: FSMContext, services):
    data = await state.get_data()
    key, sec = data.get("key"), data.get("sec", "root")
    lo, hi, _label, _unit = texts.SETTINGS_BOUNDS[key]
    raw = (message.text or "").strip()
    try:
        val = int(raw)
        if not (lo <= val <= hi):
            raise ValueError
    except ValueError:
        await message.answer(texts.settings_bad_value(key))
        return
    await call(settings.set_value, key, val)
    await state.clear()
    text, markup = _screen(sec, services)
    await message.answer(text, reply_markup=markup)


# ── выбор enum (расписание обновлений) ───────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "pick"))
async def pick(cb: CallbackQuery, callback_data: SetCB, services):
    if callback_data.sec == "upd" and callback_data.key == "sched":
        opt = callback_data.val
        await call(settings.set_value, "updates.poll_schedule", opt)
        if opt == "never":                      # никогда → авто-мьют уведомлений
            await call(services.mute_updates)
    await _render(cb, callback_data.sec, services)
    await cb.answer()


# ── действия (бэкап сейчас, рестарты, проверка обновлений) ────────────────────
@router.callback_query(SetCB.filter(F.act == "do"))
async def do_action(cb: CallbackQuery, callback_data: SetCB, services):
    key = callback_data.key
    if key == "now":                                   # бэкап сейчас
        await cb.answer("Готовлю бэкап…")
        paths = await call(services.make_backup)
        for p in paths:
            try:
                await cb.message.answer_document(FSInputFile(p))
            except Exception:                          # noqa: BLE001
                pass
        await _render(cb, "backup", services)
        return
    if key == "awg":                                   # рестарт AWG
        await cb.answer("Перезапускаю AWG…")
        try:
            await call(services.restart_service)
            await edit(cb, "✅ AmneziaWG перезапущен, блокировки восстановлены.",
                       kb.settings_svc())
        except Exception as e:                         # noqa: BLE001
            await edit(cb, f"⚠️ Ошибка перезапуска AWG: {e}", kb.settings_svc())
        return
    if key == "bot":                                   # рестарт бота
        await cb.answer("Перезапускаю бота…")
        await edit(cb, "🔄 Бот перезапускается — вернётся через несколько секунд.", None)
        await call(services.restart_bot)
        return
    if key == "check":                                 # проверить обновление сейчас
        await cb.answer("Проверяю…")
        nxt = await call(services.update_next)
        if nxt is None:
            await edit(cb, texts.update_current_ok(config.INSTALLED_VERSION),
                       kb.settings_updates(services.updates_muted()))
        else:
            await edit(cb, texts.update_admin_available(config.INSTALLED_VERSION, nxt.tag, nxt.body),
                       kb.update_admin_available())
        return
