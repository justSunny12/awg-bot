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

MARK = "NETMAP подсетей клонов не применён"


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


@pytest.mark.parametrize("why, words", [
    ("kernel", "ядро или nft не приняли NETMAP"),
    ("pool", "нет свободного блока"),
    ("bad_pool", "пул reserve.zone_pool не разобран"),
    ("", "причина не записана"),
], ids=["kernel", "pool", "bad_pool", "unknown"])
def test_the_line_names_the_one_reason_from_the_zone_state(host, monkeypatch, caplog, why, words):
    """Причину пишет скрипт (netmap_why); строка «ядро или пул» заставляла бы
    админа сверять версию ядра и маршруты руками."""
    _clone_up(host, monkeypatch, caplog, ifaces="awg0", netmap="", netmap_why=why)
    host.svc.reserve_in_tick()
    got = _marks(caplog)
    assert len(got) == 1 and words in got[0].getMessage(), [r.getMessage() for r in caplog.records]


def test_a_partial_plan_names_the_clones_without_a_block_once(host, monkeypatch, caplog):
    """Клонов больше, чем пар в плане: их устройства выходят одним адресом, а
    `netmap=` не пуст — без своей строки это не видно нигде."""
    _clone_up(host, monkeypatch, caplog, ifaces="awg0 awg1", netmap="10.8.1.0/24=169.254.96.0/24", netmap_miss="awg1")
    assert "up" in sent(host.svc.reserve_in_tick())
    part = [r for r in caplog.records if "NETMAP не на все клоны" in r.getMessage()]
    assert len(part) == 1 and part[0].levelno == logging.WARNING and "awg1" in part[0].getMessage(), \
        [r.getMessage() for r in caplog.records]
    assert not _marks(caplog), "частичный план назван «не встал»"
    for _ in range(3):
        host.svc.reserve_in_tick()
    assert len([r for r in caplog.records if "NETMAP не на все клоны" in r.getMessage()]) == 1


def test_a_clone_with_netmap_logs_nothing(host, monkeypatch, caplog):
    _clone_up(host, monkeypatch, caplog, ifaces="awg0", netmap="10.8.1.0/24=169.254.96.0/24")
    assert "up" in sent(host.svc.reserve_in_tick())
    assert not _marks(caplog), "NETMAP встал, а в журнале тревога"


def test_a_zone_state_without_clones_logs_nothing(host, monkeypatch, caplog):
    """Состояние зоны ещё без клонов (или не прочиталось) — судить не о чем."""
    _clone_up(host, monkeypatch, caplog, netmap="")
    host.svc.reserve_in_tick()
    assert not _marks(caplog)


PART = "NETMAP не на все клоны"


def test_clones_without_a_block_under_an_empty_plan_give_only_the_one_line(host, monkeypatch, caplog):
    """План пуст, а скрипт записал и `netmap_miss` (все клоны без блока): это
    один факт «NETMAP не встал», а не два — вторая строка про частичный план
    сбила бы админа (частичного плана нет, NETMAP нет ни на одном клоне)."""
    _clone_up(host, monkeypatch, caplog, ifaces="awg0 awg1", netmap="", netmap_why="pool", netmap_miss="awg0 awg1")
    assert "up" in sent(host.svc.reserve_in_tick())
    assert len(_marks(caplog)) == 1, [r.getMessage() for r in caplog.records]
    assert not [r for r in caplog.records if PART in r.getMessage()], \
        "при пустом плане ещё и строка частичного плана"


@pytest.mark.parametrize("why", ["kernel", "pool", "bad_pool"])
def test_a_reason_without_clones_in_the_zone_state_logs_nothing(host, monkeypatch, caplog, why):
    """Причина в состоянии есть, а клонов нет (состояние от прошлого раза или
    не дописано): судить не о чем — ни строки «не встал», ни частичного плана."""
    _clone_up(host, monkeypatch, caplog, netmap="", netmap_why=why, netmap_miss="awg1")
    host.svc.reserve_in_tick()
    assert not _marks(caplog) and not [r for r in caplog.records if PART in r.getMessage()], \
        [r.getMessage() for r in caplog.records]
