"""
tgsession.py — сессия Bot API для всех ролей: короткое соединение и повтор.

У aiogram один таймаут на весь запрос — 60 с, и он же ждёт установления
TCP-соединения: когда аплинк теряет часть SYN к Telegram (с ВПС и через
NAT шлюзов это наблюдалось минутами), каждое зависшее соединение держит
кнопку или уведомление минуту. Здесь установление соединения ограничено
CONNECT_S, и неудача ИМЕННО соединения (таймаут connect, отказ маршрута)
повторяется до ATTEMPTS раз: запрос при этом ещё не ушёл, повтор безопасен
для любого метода. Таймаут уже отправленного запроса и ответы Bot API —
без повторов, как прежде.

Сессия прибита к IPv4 по умолчанию у всех ролей (проект только IPv4, см.
util/net4): v6-адрес API уходит мимо туннеля на шлюзе или в никуда на ВПС.
`ipv4_only=False` оставлен выключателем — его даёт только агент по
`app.gateway.ipv4_only`.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Optional, cast

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError

log = logging.getLogger("awgbot.tgsession")

CONNECT_S = 5.0                 # на установление TCP-соединения с api.telegram.org
TOTAL_S = 10.0                  # весь запрос: правило проекта — наружу не дольше 10 с (getUpdates — свой, от aiogram)
ATTEMPTS = 3                    # попыток соединения на один запрос
RETRY_PAUSE_S = 0.5             # пауза между попытками

_CONNECT_ERRORS: tuple = (aiohttp.ServerTimeoutError, aiohttp.ClientConnectorError)


class RetryingSession(AiohttpSession):
    """AiohttpSession с ограничением на соединение и повтором неудачного connect."""

    def __init__(self, *, ipv4_only: bool = True, connect_s: float = CONNECT_S,
                 attempts: int = ATTEMPTS, **kwargs: Any) -> None:
        kwargs.setdefault("timeout", TOTAL_S)
        super().__init__(**kwargs)
        self.connect_s = float(connect_s)
        self.attempts = max(1, int(attempts))
        if ipv4_only:
            # проект только IPv4: v6-адрес API — мимо туннеля (шлюз) или в никуда (ВПС), не тратить на него и паузу перебора
            self._connector_init["family"] = socket.AF_INET

    async def make_request(self, bot, method, timeout: Optional[int] = None):
        """Тот же запрос aiogram, но с таймаутом соединения и повтором, если
        соединение не установилось (причина — в `__cause__` сетевой ошибки)."""
        total = self.timeout if timeout is None else timeout
        tmo = aiohttp.ClientTimeout(total=float(total), sock_connect=self.connect_s)
        for attempt in range(1, self.attempts + 1):
            try:
                return await super().make_request(bot, method, timeout=cast(Any, tmo))
            except TelegramNetworkError as e:
                cause = e.__cause__
                if attempt < self.attempts and isinstance(cause, _CONNECT_ERRORS):
                    log.info("Bot API: соединение не установилось (%s: %s) — попытка %d из %d",
                             type(cause).__name__, str(cause)[:80], attempt, self.attempts)
                    await asyncio.sleep(RETRY_PAUSE_S)
                    continue
                raise
        raise AssertionError("unreachable")


def make(*, ipv4_only: bool = True) -> RetryingSession:
    return RetryingSession(ipv4_only=ipv4_only)
