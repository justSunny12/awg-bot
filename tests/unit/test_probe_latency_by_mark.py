"""Зонды маршрутизации (infra/routing/probes.py): задержка последнего удавшегося
коннекта хранится по метке — зонд фичи (ROUTING_FWMARK) и зонды слотов
(1 << slot) ходят разными путями, и один не затирает отметку другого.

Цена ошибки: доктор маршрутизации показывает задержку не того шлюза — «через
шлюз 900 мс» о здоровом пути после зонда слота через медленный.
"""
from __future__ import annotations

import socket

import pytest

from awgbot.core import config
from awgbot.infra.routing import probes

pytestmark = pytest.mark.unit


class _Sock:
    calls: list[tuple[int, str]] = []

    def __init__(self, *a, **k):
        self.mark = None

    def setsockopt(self, level, opt, value):
        self.mark = value

    def settimeout(self, t):
        pass

    def connect(self, addr):
        _Sock.calls.append((self.mark, addr[0]))
        if addr[0] == "10.0.0.99":
            raise OSError("нет пути")

    def close(self):
        pass


def test_each_mark_keeps_its_own_latency(monkeypatch):
    monkeypatch.setattr(socket, "socket", _Sock)
    monkeypatch.setattr(probes, "_last_probe_ms", {})
    assert probes._tcp_probe("1.1.1.1", 443, 1.0) is True                 # зонд фичи
    feature = probes.last_probe_latency_ms()
    assert feature is not None
    assert probes._tcp_probe("10.0.0.99", 443, 1.0, mark=1 << 3) is False  # зонд слота 3 — мимо
    assert probes.last_probe_latency_ms(1 << 3) is None
    assert probes.last_probe_latency_ms() == feature, "зонд слота затёр отметку зонда фичи"
    assert probes.last_probe_latency_ms(config.ROUTING_FWMARK) == feature
