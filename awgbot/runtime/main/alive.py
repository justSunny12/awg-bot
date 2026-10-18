"""alive.py — часть пакета main (см. __init__): отметка жизни для сторожа
обновления по Telegram — первый ответ Bot API на getUpdates (опрос пошёл).

Обе роли. getMe на старте не в счёт: процесс, ответивший на него, ещё может не
дойти до опроса. Отказ getUpdates (сеть, 401) — исключение из запроса, отметки
нет. Второй признак агента — подписанное слово сервера AWG в канале
(runtime/linkclient/session.py); какой первый — тот и отмечает (marks.py).
"""
from __future__ import annotations

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import GetUpdates

from .common import log


class AliveOnPolling(BaseRequestMiddleware):
    """Посредник запросов Bot API: ответ на getUpdates — отметка жизни."""

    def __init__(self) -> None:
        self.done = False

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        if not self.done and isinstance(method, GetUpdates):
            self.done = True
            from awgbot.domain.selfupdate.marks import mark_alive
            mark_alive("Telegram ответил на getUpdates")
        return result


def watch(bot) -> None:
    """Повесить посредник на сессию бота; сессия без посредников (двойник
    тестов) — ничего."""
    try:
        bot.session.middleware(AliveOnPolling())
    except Exception as e:                               # noqa: BLE001
        log.warning("отметка жизни по Telegram не подключена: %s", e)
