"""bgtask.py — фоновые задачи бота под присмотром.

asyncio.create_task без присмотра теряет исключение: задача падает молча, в
журнале лишь «Task exception was never retrieved» при сборке мусора, а то и
ничего. spawn — та же задача, но с обработчиком завершения: исключение идёт в
журнал с трассой сразу, отмена — не ошибка. Держатель ссылки — вызывающий (как
и с create_task): задача без ссылки может быть собрана, не доработав.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Coroutine

log = logging.getLogger("awgbot.bot.bgtask")


def _done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    e = task.exception()
    if e is not None:
        log.error("фоновая задача %s упала: %s: %s", task.get_name(), type(e).__name__, e, exc_info=e)


def spawn(coro: Coroutine, *, name: str = "") -> asyncio.Task:
    """create_task + журнал исключения по завершении."""
    task = asyncio.get_running_loop().create_task(coro, name=name or None)
    task.add_done_callback(_done)
    return task


__all__ = ["spawn"]
