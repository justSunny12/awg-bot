"""«Скрыть» — универсальная последняя кнопка на ЛЮБОМ проактивном уведомлении
(см. notifier.py), следе события и погашенном меню с итогом, у обеих ролей.
Роль-агностик: UI-действие над своим же сообщением. Удаляет само сообщение
(не просто прячет клавиатуру — так уведомление реально пропадает из чата, а
не висит пустым текстом) и тем же действием снимает его снимки списков и
запись (bot/lists/msgs/hide). Кнопка живёт сутки: такт после полуночи
снимает её раньше, чем Telegram перестанет удалять (48 часов); отказ
удаления — молча (бот не работал дольше суток — кнопку снимет догон)."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery

from awgbot.bot.callbacks import HideCB

async def on_hide(cb: CallbackQuery, services=None):
    try:
        await cb.message.delete()
        if services is not None:                      # спрятали живое меню — указатель долой
            from awgbot.bot.handlers.common import forget_nav_if
            from awgbot.bot.lists.msgs import hide as msgs_hide
            await forget_nav_if(services, cb.message.chat.id, cb.message.message_id)
            await msgs_hide.on_hidden(services, cb.message.chat.id, cb.message.message_id)   # снимки и запись — прочь
    except Exception:                                 # noqa: BLE001
        pass                                          # уже удалено/бот без прав — не страшно
    await cb.answer()


def make_router() -> Router:
    """Свой Router на каждый диспетчер: один и тот же в два не включить."""
    router = Router(name="hide")
    router.callback_query(HideCB.filter())(on_hide)
    return router
