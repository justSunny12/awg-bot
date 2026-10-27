"""router.py — часть пакета lists (см. __init__): колбэк ListCB — экран вида
на месте сообщения (листание отдаёт его заново через PageCB, «⬅️ Назад» с
листаемого источника — тоже). Роль сверяется здесь: роутер стоит до роутеров
ролей и их фильтров не наследует."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery

from awgbot.bot.callbacks import ListCB


async def list_screen(cb: CallbackQuery, callback_data: ListCB, services, role: str = "", client=None):
    from awgbot.bot import lists, paging
    from awgbot.bot.handlers.common import edit_nav
    d = callback_data
    page = paging.page_of(cb.message.chat.id, lists.screen_name(d.kind), d.ref)
    parts = await lists.render(services, d.kind, role=role, ref=d.ref, key=d.key, new=bool(d.new), page=page,
                               chat_id=cb.message.chat.id)
    if parts is None:
        await cb.answer()                              # вида у роли нет — молча, как у устаревшей кнопки
        return
    await edit_nav(cb, services, *parts)
    await cb.answer()


def make_router() -> Router:
    """Свой Router на каждый диспетчер (у обеих установок свой)."""
    router = Router(name="lists")
    router.callback_query(ListCB.filter())(list_screen)
    return router
