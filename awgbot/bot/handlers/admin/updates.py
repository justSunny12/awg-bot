"""
handlers/admin/updates.py — обновления бота (self-update): установка, меню
итога, выключение уведомлений.
"""

from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from awgbot.bot.callbacks import UpdateCB
from awgbot.bot.handlers.common import call, dismiss_update_reports, cleanup_content, show_main_menu
from awgbot.bot.handlers.admin.panel import _return_panel

router = Router(name="admin.updates")


# ── Обновления бота (self-update) ────────────────────────────────────────────

@router.callback_query(UpdateCB.filter(F.action == "install"))
async def update_install(cb: CallbackQuery, services):
    """«Обновить» (из уведомления или меню обновления). Целевой поток:
    стереть цепочку до этого шага ВКЛЮЧИТЕЛЬНО (контент-сообщения + само
    сообщение с кнопкой), оставить единственное «дождись завершения» (без
    кнопок), запомнить его для удаления после рестарта и запустить апдейтер.
    Итог («успешно обновлен…» + changelog + «В меню») пришлёт новый процесс."""
    nxt = await call(services.update_next)
    if nxt is None:
        await cb.answer("Обновлять не на что — версия актуальна.", show_alert=True)
        return
    blocked = await call(services.update_block_reason, nxt)
    if blocked:
        await cb.answer(f"Нельзя: {blocked}", show_alert=True)
        return
    await cb.answer("Запускаю обновление…")
    chat_id = cb.message.chat.id
    await cleanup_content(cb.bot, services, chat_id)
    try:
        await cb.message.delete()                     # сам шаг — тоже в утиль
    except Exception:                                 # noqa: BLE001
        pass
    wait = await cb.bot.send_message(chat_id, texts.update_wait(nxt.tag))
    await call(services.set_update_wait, chat_id, wait.message_id)
    try:
        await call(services.apply_update, nxt)
        # успех: апдейтер вот-вот остановит сервис; «дождись» удалит и итог
        # пришлёт уже новый процесс (confirm_applied_update на старте).
    except Exception as e:                            # noqa: BLE001
        # не взлетело ещё ДО апдейтера (сеть/sha256/запись файла) — прибраться:
        # wait-сообщение, wait-ссылка и pending-флаг (иначе следующий рестарт
        # принесёт ложный «не применилось»)
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
        await _return_panel(cb.message, services)


@router.callback_query(UpdateCB.filter(F.action == "menu"))
async def update_menu(cb: CallbackQuery, services, state: FSMContext):
    """«В меню» на итоговом сообщении self-update: текст остаётся в истории,
    снимаем только клавиатуру; меню — новым сообщением."""
    await cb.answer()
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                 # noqa: BLE001
        pass
    # и у всех прочих окон обновления тоже — живой должна быть одна кнопка;
    # текущее уже погашено выше — второй раз не трогаем
    await dismiss_update_reports(cb.bot, services,
                                 keep=(cb.message.chat.id, cb.message.message_id))
    await show_main_menu(cb.message, services, "admin")


@router.callback_query(UpdateCB.filter(F.action == "mute"))
async def update_mute(cb: CallbackQuery, services):
    """«Не уведомлять об обновлениях» — глушит автоуведомления и стартовую
    проверку. Само уведомление убираем; ручная кнопка остаётся живой."""
    await call(services.mute_updates)
    await cb.answer("Уведомления об обновлениях выключены.")
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass
