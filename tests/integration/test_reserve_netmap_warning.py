"""Строка в журнале бота резерва, когда NETMAP подсетей клонов не встал
(domain/services/reserve/inbound.py `_tick_taking`): процесс открытия вывод
`--take` не пересказывает, поэтому факт берётся из состояния зоны — клоны
есть (`ifaces`), а `netmap` пуст.

Зона и процесс открытия подставные (фикстура `host` из test_reserve_hosting).

Цена ошибки: без строки админ резерва не узнаёт, что устройства основы
вышли одним адресом зоны и CAKE делит канал по назначению, — жалобы «на
резерве всё медленно» без следа в журнале; строка на каждом такте —
журнал забит одной и той же строкой весь переезд; строка при вставшем
NETMAP — ложная тревога.
"""
from __future__ import annotations

import logging

import pytest

from awgbot.infra import rsvzone

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import sent
from tests.integration.test_reserve_hosting import _taking, host

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host", "mv", "rsv"]

MARK = "NETMAP подсетей клонов не встал"


def _clone_up(host, monkeypatch, caplog, **zone_state):
    """Переезд до шага clone_up с заданным состоянием зоны; журнал — с уровня WARNING."""
    _taking(host)
    monkeypatch.setattr(rsvzone, "state", lambda *a: {"phase": "taking", "iface": "awg0", **zone_state})
    host.zone.status = {"step": "clone_up"}
    host.spawn.rc = None
    caplog.clear()
    caplog.set_level(logging.WARNING)


def _marks(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if MARK in r.getMessage()]


def test_a_clone_without_netmap_is_logged_once_as_a_warning(host, monkeypatch, caplog):
    _clone_up(host, monkeypatch, caplog, ifaces="awg0 awg1", netmap="")
    assert "up" in sent(host.svc.reserve_in_tick()), "клон поднят, а основе не сказано up"
    got = _marks(caplog)
    assert len(got) == 1 and got[0].levelno == logging.WARNING, [r.getMessage() for r in caplog.records]
    for _ in range(3):
        host.svc.reserve_in_tick()
    assert len(_marks(caplog)) == 1, "строка повторяется на каждом такте переезда"


def test_a_clone_with_netmap_logs_nothing(host, monkeypatch, caplog):
    _clone_up(host, monkeypatch, caplog, ifaces="awg0", netmap="10.8.1.0/24=169.254.96.0/24")
    assert "up" in sent(host.svc.reserve_in_tick())
    assert not _marks(caplog), "NETMAP встал, а в журнале тревога"


def test_a_zone_state_without_clones_logs_nothing(host, monkeypatch, caplog):
    """Состояние зоны ещё без клонов (или не прочиталось) — судить не о чем."""
    _clone_up(host, monkeypatch, caplog, netmap="")
    host.svc.reserve_in_tick()
    assert not _marks(caplog)
