"""Локальный канал бот резерва ↔ бот основы на резерве (runtime/pairlink/guestctl.py)
по настоящему unix-сокету — правка по ревью 4.1.0: исключение обработчика (замок
базы гостя дольше busy_timeout при применении событий основы, любая другая ошибка)
не убивает клиента гостя, а закрывает сессию строкой журнала — гость переподключается
и получает своё снова; у слушателя бота резерва — то же, со строкой в журнале.

Мир — tests/integration/test_reserve_guestctl.py (сервисы обеих сторон — заглушки).

Цена ошибки: задача клиента, умершая на «database is locked», — гость больше не
подключается к боту резерва: не уходят online и tg_taken, не приходят released,
takeover и stop; на возврате через 90 с — released без эпохи, основа берёт эпоху
без правок гостя, а они стираются с `--wipe` резерва.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
import types

import pytest

from awgbot.runtime.pairlink import guestctl

from tests.integration.test_reserve_guestctl import _GuestSvc, _ResSvc, _until

pytestmark = pytest.mark.integration

_JEV = {"line": "00" * 8, "e": 1, "from": 1, "n": 1, "blob": ""}


class _JrnGuest(_GuestSvc):
    """Гость: первые `fail` вызовов применения падают с ошибкой базы."""

    def __init__(self, fail: int = 1, exc: Exception | None = None):
        super().__init__()
        self.fail, self.exc, self.calls = fail, exc or sqlite3.OperationalError("database is locked"), 0

    def guest_jrn_status(self):
        return {}

    def guest_jrn_in(self, kind, msg):
        self.calls += 1
        if self.calls <= self.fail:
            raise self.exc
        return [("jack", {"line": msg["line"], "i": msg["from"]})]


class _JrnRes(_ResSvc):
    def __init__(self, fail_words: int = 0):
        super().__init__()
        self.words: list[tuple] = []
        self.fail_words = fail_words

    def reserve_guest_jrn_word(self, kind, body):
        if self.fail_words:
            self.fail_words -= 1
            raise sqlite3.OperationalError("database is locked")
        self.words.append((kind, dict(body)))
        return []


async def _world(res, gst):
    d = tempfile.mkdtemp(prefix="rg", dir="/tmp")
    path = os.path.join(d, "g.sock")
    server = guestctl.GuestCtlServer(res, path)
    client = guestctl.GuestCtlClient(gst, path)
    await server.ensure()
    client.start()
    return types.SimpleNamespace(server=server, client=client, res=res, guest=gst, dir=d)


async def _close(w) -> None:
    await w.client.stop()
    await w.server.stop()
    shutil.rmtree(w.dir, ignore_errors=True)


@pytest.mark.parametrize("exc", [sqlite3.OperationalError("database is locked"), OverflowError("int too big"),
                                 RuntimeError("что угодно")])
async def test_a_failing_handler_does_not_kill_the_guest_client_and_it_reconnects(exc, caplog):
    """Обработчик гостя упал — сессия закрыта строкой журнала, задача клиента жива,
    через пару секунд гость снова на связи; повтор того же слова применяется и
    подтверждается."""
    w = await _world(_JrnRes(), _JrnGuest(fail=1, exc=exc))
    try:
        assert await _until(lambda: w.server.connected(), 3)
        first = w.res.calls
        caplog.set_level(logging.WARNING)
        assert await w.server.send("jev", _JEV) is True
        assert await _until(lambda: w.guest.calls == 1, 3)
        assert await _until(lambda: w.res.calls > first, 8), "гость не переподключился после падения обработчика"
        assert not w.client._task.done(), f"задача клиента умерла: {w.client._task}"
        assert any("обработчик упал" in r.getMessage() for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
        assert await _until(lambda: w.server.connected(), 3)
        assert await w.server.send("jev", _JEV) is True
        assert await _until(lambda: [k for k, _ in w.res.words] == ["jack"], 3), \
            f"повтор после переподключения не подтверждён: {w.res.words}"
    finally:
        await _close(w)


async def test_a_failing_word_handler_at_the_reserve_closes_the_session_and_the_guest_comes_back(caplog):
    """У слушателя бота резерва разбор слова гостя упал — сессия закрыта строкой журнала
    (не тишиной), гость переподключается, следующее слово доходит."""
    w = await _world(_JrnRes(fail_words=1), _JrnGuest(fail=0))
    try:
        assert await _until(lambda: w.server.connected() and w.client.connected(), 3)
        first = w.res.calls
        caplog.set_level(logging.WARNING)
        assert await w.client.send("jack", {"line": "00" * 8, "i": 1}) is True
        assert await _until(lambda: w.res.calls > first, 8), "гость не вернулся после сбоя у слушателя"
        assert any("локальный канал гостя: обработчик упал" in r.getMessage() for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
        assert await _until(lambda: w.client.connected(), 3)
        assert await w.client.send("jack", {"line": "00" * 8, "i": 2}) is True
        assert await _until(lambda: [b["i"] for _, b in w.res.words] == [2], 3), w.res.words
    finally:
        await _close(w)
