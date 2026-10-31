"""Отправка в канал пары (runtime/pairlink/common.py `send_line`): drain, не
уложившийся в срок или сорвавшийся, закрывает сессию — она мертва, следующая
начнётся с `st`.

Цена ошибки: недописанное в буфере сокета доходит позже и перемешивается со
словами следующего поколения пакета; `released` «не ушла — поллинг обратно»,
а резерв реплику всё же получает — 409 и два бота на одно обновление.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.runtime.pairlink import common as plc

pytestmark = pytest.mark.unit


class _Writer:
    def __init__(self, drain_exc=None):
        self.drain_exc = drain_exc
        self.closed = False
        self.lines: list[bytes] = []

    def write(self, line):
        self.lines.append(line)

    async def drain(self):
        if self.drain_exc is not None:
            raise self.drain_exc
        await asyncio.sleep(3600)                           # буфер не уходит

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


async def test_a_hung_drain_closes_the_session(monkeypatch):
    monkeypatch.setattr(plc, "SEND_TIMEOUT", 0.05)
    w = _Writer()
    sess = plc._Session(w, b"k" * 32, listener=True)
    assert await plc.send_line(sess, "pkg", {"gen": 1}) is False
    assert w.closed, "сессия с недописанным буфером жива — хвост дойдёт позже"


async def test_a_broken_drain_closes_the_session():
    w = _Writer(ConnectionResetError("сброс"))
    sess = plc._Session(w, b"k" * 32, listener=True)
    assert await plc.send_line(sess, "released", {}) is False
    assert w.closed
