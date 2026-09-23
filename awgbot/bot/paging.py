"""
paging.py — листание длинных списков кнопок.

Правило одно на весь интерфейс: не больше десяти кнопок на экране. Списки,
которые в десятку не влезают, режутся на страницы (keyboards.common.page_slice),
а страницу помнит этот модуль — по чату и имени списка, в памяти процесса.
Не в FSM-данных намеренно: почти каждый хендлер начинает со state.clear(), и
страница сбрасывалась бы на каждом действии в списке.

Кнопка листания несёт PageCB: имя списка, страницу и упакованный колбэк,
который рисует экран. Хендлер запоминает страницу и скармливает диспетчеру
тот же колбэк заново — экран перерисовывается своим хендлером, с той
страницы. Рестарт бота теряет страницы: список откроется с первой.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.types import CallbackQuery, Update

from awgbot.bot.callbacks import PageCB

log = logging.getLogger(__name__)

router = Router(name="paging")
_pages: dict[tuple[int, str, int], int] = {}


def page_of(chat_id: int, screen: str, ref: int = 0) -> int:
    return _pages.get((int(chat_id or 0), screen, int(ref or 0)), 0)


def remember(chat_id: int, screen: str, ref: int, page: int) -> None:
    _pages[(int(chat_id or 0), screen, int(ref or 0))] = max(0, int(page))


@router.callback_query(PageCB.filter())
async def turn_page(cb: CallbackQuery, callback_data: PageCB, dispatcher: Dispatcher,
                    bot: Bot, event_update: Update):
    remember(cb.message.chat.id, callback_data.screen, callback_data.ref, callback_data.page)
    if not callback_data.back:
        await cb.answer()
        return
    # тот же экран, тем же хендлером, с новой страницы: подменяем data колбэка
    # и отдаём диспетчеру как новое событие
    again = cb.model_copy(update={"data": callback_data.back})
    await dispatcher.feed_update(bot, Update(update_id=event_update.update_id, callback_query=again))
    try:
        await cb.answer()
    except Exception:                                     # noqa: BLE001
        pass                                              # хендлер экрана уже ответил
