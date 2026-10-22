"""Сессия Bot API (runtime/tgsession): короткий таймаут соединения и повтор
неудачного connect.

Цена ошибки: без короткого connect каждое зависшее соединение с Telegram
держит кнопку или уведомление минуту; повтор не того класса ошибок —
дважды отправленное сообщение (запрос уже ушёл) или проглоченная ошибка
Bot API; сессия без AF_INET на шлюзе — попытка по v6 в никуда через
v4-туннель.
"""
from __future__ import annotations

import asyncio
import socket

import aiohttp
import pytest
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetMe
from aiohttp.client_reqrep import ConnectionKey

from awgbot.runtime import tgsession

pytestmark = pytest.mark.unit


def _connect_refused() -> aiohttp.ClientConnectorError:
    key = ConnectionKey("api.telegram.org", 443, True, None, None, None, None, None)
    return aiohttp.ClientConnectorError(key, OSError(113, "No route to host"))


class _Api:
    """Подмена базового make_request: очередь исходов по попыткам.

    Исход — исключение (поднимается; `cause` ставится в `__cause__`, как
    это делает aiogram через `raise ... from`) или значение (возвращается).
    Копит переданные таймауты — по ним видно, что ушло в aiohttp."""

    def __init__(self, monkeypatch, *outcomes):
        self.outcomes = list(outcomes)
        self.timeouts: list = []
        api = self

        async def _make_request(session, bot, method, timeout=None):
            api.timeouts.append(timeout)
            out = api.outcomes.pop(0)
            if isinstance(out, BaseException):
                cause = getattr(out, "_cause", None)
                if cause is not None:
                    raise out from cause
                raise out
            return out

        monkeypatch.setattr(AiohttpSession, "make_request", _make_request)
        monkeypatch.setattr(tgsession, "RETRY_PAUSE_S", 0)

    @property
    def calls(self) -> int:
        return len(self.timeouts)


def _net(cause: BaseException | None) -> TelegramNetworkError:
    e = TelegramNetworkError(method=GetMe(), message="сеть")
    e._cause = cause                                            # noqa: SLF001 — см. _Api
    return e


def _bot() -> Bot:
    return Bot("123:DUMMY", session=tgsession.make())


async def test_a_connect_timeout_is_retried_three_times_with_a_short_connect_limit(monkeypatch):
    """Соединение не установилось трижды — три попытки, у каждой connect 5 с
    при общем пределе 10 с (правило проекта: наружу не дольше 10 с), и наружу
    уходит та же сетевая ошибка."""
    api = _Api(monkeypatch, *(_net(aiohttp.ServerTimeoutError("connect")) for _ in range(3)))
    with pytest.raises(TelegramNetworkError):
        await _bot()(GetMe())
    assert api.calls == 3, "неудачное соединение не повторилось положенные три раза"
    for tmo in api.timeouts:
        assert isinstance(tmo, aiohttp.ClientTimeout), "в aiohttp ушёл не ClientTimeout"
        assert tmo.sock_connect == 5.0, "установление соединения не ограничено 5 с"
        assert tmo.total == 10.0, "общий предел запроса отличается от 10 с правила проекта"


async def test_a_refused_route_is_retried_and_the_second_attempt_returns_the_answer(monkeypatch):
    """Первая попытка — нет маршрута, вторая — ответ: вызывающий получает ответ,
    а не ошибку; запрос при первой попытке не ушёл, повтор безопасен."""
    api = _Api(monkeypatch, _net(_connect_refused()), {"id": 1, "is_bot": True, "first_name": "b"})
    assert await _bot()(GetMe()) == {"id": 1, "is_bot": True, "first_name": "b"}
    assert api.calls == 2, "после удачной попытки сессия продолжила пробовать"


async def test_a_timeout_of_a_sent_request_is_not_retried(monkeypatch):
    """Общий таймаут (запрос мог дойти до Telegram) — без повтора: иначе
    сообщение уходит дважды."""
    api = _Api(monkeypatch, _net(asyncio.TimeoutError()), {"id": 1})
    with pytest.raises(TelegramNetworkError):
        await _bot()(GetMe())
    assert api.calls == 1, "таймаут уже отправленного запроса повторился"


async def test_an_api_error_is_not_retried(monkeypatch):
    """Ответ Bot API с ошибкой — не сеть: пробрасывается с первого раза."""
    api = _Api(monkeypatch, TelegramBadRequest(method=GetMe(), message="Bad Request: chat not found"), {"id": 1})
    with pytest.raises(TelegramBadRequest):
        await _bot()(GetMe())
    assert api.calls == 1, "ошибка Bot API повторилась как сетевая"


async def test_a_network_error_without_a_cause_is_not_retried(monkeypatch):
    """Сетевая ошибка без причины (не connect) — без повтора."""
    api = _Api(monkeypatch, _net(None), {"id": 1})
    with pytest.raises(TelegramNetworkError):
        await _bot()(GetMe())
    assert api.calls == 1


def test_the_session_is_pinned_to_ipv4_by_default():
    """Правило проекта: наружу только IPv4. Сессия без параметров — AF_INET у
    обеих ролей; иначе резолвер с AAAA уводит Bot API мимо туннеля (шлюз) или
    в никуда (ВПС), и каждый запрос стоит паузу перебора."""
    assert tgsession.make()._connector_init["family"] == socket.AF_INET               # noqa: SLF001
    assert tgsession.RetryingSession()._connector_init["family"] == socket.AF_INET    # noqa: SLF001


def test_ipv4_only_false_lifts_the_pin():
    """Явное `ipv4_only=False` снимает привязку — семейство не ограничено."""
    assert "family" not in tgsession.make(ipv4_only=False)._connector_init            # noqa: SLF001


def test_the_default_total_limit_is_ten_seconds():
    """Правило проекта: запрос наружу — не дольше 10 с. Сессия без явного
    таймаута берёт именно этот предел, а не 60 с aiogram по умолчанию —
    иначе зависший запрос к Telegram держит кнопку минуту."""
    assert tgsession.TOTAL_S == 10.0
    assert tgsession.make().timeout == 10.0, "сессия не взяла TOTAL_S общим пределом запроса"


async def test_an_explicit_timeout_becomes_the_total_limit(monkeypatch):
    """Явный таймаут вызова (как у long polling) — общий предел запроса и
    приоритетнее TOTAL_S, connect при этом остаётся коротким."""
    api = _Api(monkeypatch, {"id": 1})
    await _bot()(GetMe(), request_timeout=25)
    assert api.timeouts[0].total == 25.0
    assert api.timeouts[0].sock_connect == 5.0


async def test_the_test_suite_network_guard_still_closes_the_subclass():
    """Заглушка conftest над AiohttpSession.make_request срабатывает и через
    RetryingSession: ни один тест не ходит в Telegram."""
    with pytest.raises(TelegramNetworkError, match="сеть в тестах закрыта"):
        await _bot()(GetMe())
