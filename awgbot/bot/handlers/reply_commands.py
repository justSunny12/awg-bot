"""
handlers/reply_commands.py — reply-команда «Отмена» у поля ввода.

Одна кнопка «✖️ Отмена» показывается ТОЛЬКО во время текстового ввода (её несёт
приглашение к вводу). ОТДЕЛЬНЫЙ роутер, зарегистрирован ПЕРВЫМ, фильтр строго по
точному тексту + StateFilter("*") — бьёт раньше FSM-хендлеров (иначе «Отмена» на
шаге ввода имени записалась бы как имя). Обычный текст под фильтр не попадает.

Обработка намерения живёт здесь (в хендлере, по SRP); middleware остаётся тонким
охранником. Возврат в меню делегируется общему диспетчеру show_main_menu.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import HideCB
from awgbot.bot.handlers.common import show_main_menu

router = Router(name="reply_commands")


@router.callback_query(HideCB.filter())
async def on_hide(cb: CallbackQuery):
    """«Скрыть» — универсальная последняя кнопка на ЛЮБОМ проактивном
    уведомлении (см. notifier.py). Роль-агностик: работает для всех (клиент,
    друг, админ), т.к. это чисто UI-действие над своим же сообщением, доступа
    к данным не требует. Удаляет само сообщение (не просто прячет клавиатуру —
    так уведомление реально пропадает из чата, а не висит пустым текстом)."""
    try:
        await cb.message.delete()
    except Exception:                                 # noqa: BLE001
        pass                                          # уже удалено/бот без прав — не страшно
    await cb.answer()


@router.message(F.text == kb.BTN_CANCEL, StateFilter("*"))
async def on_cancel(message: Message, state: FSMContext, services,
                    role: str = "", client=None):
    """✖️ Отмена — прервать текстовый диалог в любом состоянии. «Отменено.» несёт
    снятие reply-клавы (чтобы «Отмена» не висела), затем — главное меню роли."""
    await state.clear()
    await message.answer("Отменено.", reply_markup=kb.reply_hide())
    await show_main_menu(message, services, role, client)


__all__ = ["router"]
