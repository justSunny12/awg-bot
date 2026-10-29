"""
handlers/updates_flow.py — кнопки обновлений основного бота вокруг набора:
«В меню» на итоге, «Не уведомлять», тумблер и расписание. Диалог и ход
набора — handlers/fleet.
"""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.bot.handlers.common import call, dismiss_update_reports
from awgbot.core import settings


async def menu(cb: CallbackQuery, services, state: FSMContext, *, return_panel) -> None:
    """«В меню» на итоге self-update: текст остаётся в истории с одной «Скрыть»
    (след на сутки), у прочих окон обновления кнопки снимаются (живой должна
    быть одна кнопка); меню — новым сообщением."""
    from awgbot.bot import keyboards as kb
    await cb.answer()
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.hide_only())
    except Exception:                                 # noqa: BLE001
        pass
    await dismiss_update_reports(cb.bot, services,
                                 keep=(cb.message.chat.id, cb.message.message_id))
    await return_panel(cb.message, services)


async def mute(cb: CallbackQuery, services) -> None:
    """«Не уведомлять об обновлениях» на уведомлении — глушит автоуведомления
    и стартовую проверку; само уведомление убираем, ручная кнопка в разделе
    остаётся живой."""
    await call(services.mute_updates)
    await cb.answer(texts.UPDATES_MUTED_TOAST)
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass


async def toggle_mute(cb: CallbackQuery, services) -> None:
    """Тумблер уведомлений в разделе: мьют в БД, не YAML; проверка по
    расписанию идёт в любом случае — ради строки «⬆️ Доступна vX»."""
    muted = await call(services.updates_muted)
    if muted:
        await call(services.unmute_updates)
    else:
        await call(services.mute_updates)
    await cb.answer(texts.updates_notify_toast(enabled=muted))


async def set_schedule(cb: CallbackQuery, services, opt: str) -> bool:
    """Расписание проверки: day|week|month; «никогда» из сообщений прежних
    выпусков — «месяц» и уведомления выкл (расписания «никогда» больше нет).
    False — записать не удалось, ответ колбэку уже дан."""
    want = opt if opt in ("day", "week", "month") else "month"
    try:
        await call(settings.set_value, "updates.poll_schedule", want)
    except settings.SettingsWriteError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return False
    if opt == "never":
        await call(services.mute_updates)
    return True
