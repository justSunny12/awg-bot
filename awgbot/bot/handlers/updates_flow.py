"""
handlers/updates_flow.py — self-update одним слоем для обеих ролей: установка,
«В меню» на итоге, «Не уведомлять», тумблер и расписание. Роли различаются
только тем, куда возвращают меню (return_panel), — остальное было двумя
почти построчными копиями с разошедшимися текстами.
"""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.bot.handlers.common import call, cleanup_content, dismiss_update_reports, drop_message
from awgbot.core import settings


class CachedTarget:
    """Цель обновления по сохранённому тегу — для перерисовки раздела без сети."""

    def __init__(self, tag: str):
        self.tag, self.body = tag, ""


async def install(cb: CallbackQuery, services, *, return_panel) -> None:
    """«Обновить» (из уведомления или раздела): стереть цепочку до этого шага
    ВКЛЮЧИТЕЛЬНО (контент + само сообщение с кнопкой), оставить единственное
    «дождись завершения» без кнопок, запомнить его для удаления после рестарта
    и запустить апдейтер вне cgroup. Итог («обновлено…» + changelog + «В меню»)
    пришлёт новый процесс (report_update_result на старте). Отказ ДО апдейтера
    (сеть/sha256/запись) — прибраться и вернуть меню: под кнопкой его уже нет."""
    nxt = await call(services.update_next)
    if nxt is None:
        await cb.answer(texts.UPDATE_NOTHING, show_alert=True)
        return
    blocked = await call(services.update_block_reason, nxt)
    if blocked:
        await cb.answer(texts.update_blocked_toast(blocked), show_alert=True)
        return
    await cb.answer(texts.UPDATE_STARTING)
    chat_id = cb.message.chat.id
    await cleanup_content(cb.bot, services, chat_id)
    await drop_message(cb, services)                  # сам шаг — тоже в утиль
    wait = await cb.bot.send_message(chat_id, texts.update_wait(nxt.tag))
    await call(services.set_update_wait, chat_id, wait.message_id)
    try:
        await call(services.apply_update, nxt)
    except Exception as e:                            # noqa: BLE001
        # wait-сообщение, wait-ссылка и pending-флаг (иначе следующий рестарт
        # принесёт ложный «не применилось»)
        await call(services.pop_update_wait)
        await call(services.clear_update_pending)
        try:
            await wait.delete()
        except Exception:                             # noqa: BLE001
            pass
        await cb.bot.send_message(chat_id, texts.update_failed(str(e)),
                                  reply_markup=kb.hide_only())
        await return_panel(cb.message, services)


async def menu(cb: CallbackQuery, services, state: FSMContext, *, return_panel) -> None:
    """«В меню» на итоге self-update: текст остаётся в истории, снимается
    только клавиатура — у этого и у всех прочих окон обновления (живой должна
    быть одна кнопка); меню — новым сообщением."""
    await cb.answer()
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
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
