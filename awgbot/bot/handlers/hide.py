"""«Скрыть» — универсальная последняя кнопка на ЛЮБОМ проактивном уведомлении
(см. notifier.py), у обеих ролей. Роль-агностик: чисто UI-действие над своим же
сообщением, доступа к данным не требует. Удаляет само сообщение (не просто
прячет клавиатуру — так уведомление реально пропадает из чата, а не висит
пустым текстом)."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery

from awgbot.bot.callbacks import HideCB

async def on_hide(cb: CallbackQuery, services=None):
    try:
        await cb.message.delete()
        if services is not None:                      # спрятали живое меню — указатель долой
            from awgbot.bot.handlers.common import forget_nav_if
            await forget_nav_if(services, cb.message.chat.id, cb.message.message_id)
    except Exception:                                 # noqa: BLE001
        pass                                          # уже удалено/бот без прав — не страшно
    await cb.answer()


def make_router() -> Router:
    """Свой Router на каждый диспетчер: один и тот же в два не включить."""
    router = Router(name="hide")
    router.callback_query(HideCB.filter())(on_hide)
    return router
