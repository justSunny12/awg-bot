"""
handlers/stale.py — обработчик устаревшей кнопки.

Регистрируется ПОСЛЕДНИМ: сюда падает любой колбэк, который не разобрал ни
один экран, — кнопка из меню прежней версии (другой набор полей у
CallbackData), снятое действие, чужая роль. Без него такая кнопка оставалась
со спиннером. Ответ один: всплывашка и главная роли новым сообщением.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery

STALE_BUTTON = "Кнопка устарела — открыл меню"


async def answer_stale(cb: CallbackQuery, services, role: str = "", client=None, show_main=None) -> None:
    """Ответ на устаревшую кнопку — один на всех: всплывашка, кнопки под
    сообщением — прочь, главная роли новым сообщением. Его зовут и
    обработчики, которые узнают снятое действие своего экрана. show_main —
    как у make_router; без него — главная основного бота."""
    if show_main is None:
        from awgbot.bot.handlers.common import show_main_menu as show_main
    await cb.answer(STALE_BUTTON)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                  # noqa: BLE001
        pass
    await show_main(cb.message, services, role, client)


def make_router(show_main) -> Router:
    """show_main(message, services, role, client) — главная роли новым
    сообщением; у основного бота — handlers.common.show_main_menu, у агента —
    его панель."""
    router = Router(name="stale")

    @router.callback_query()
    async def stale_button(cb: CallbackQuery, services, role: str = "", client=None):
        await answer_stale(cb, services, role, client, show_main)

    router.stale_button = stale_button                 # для тестов
    return router


__all__ = ["make_router", "answer_stale", "STALE_BUTTON"]
