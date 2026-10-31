"""Правки по ревью — голосующий кворума на шлюзе (domain/gateway/reserve/quorum.py и
соседи): первое слово новой сессии гостевого канала по номеру сессии, а не только по
флагу online раз в такт.

Хост и часы — как в tests/unit/test_gw_quorum.py (`_Host`, `_Clock`), quorum.json и
reserve.json — во временном каталоге, ответы лидера — настоящие датаграммы.

Цена ошибки: гостевой канал оборвался и поднялся между тактами — шлюз не замечает новой
сессии и молчит о своём взгляде; у резерва взгляды только в памяти, прежнее «нет» залипает
на живом канале голосом в счёте кандидата — ложное большинство и переезд при живой основе.
"""
from __future__ import annotations

import pytest

from tests.unit.test_gw_quorum import clock, gw, host
from tests.unit.test_quorum_cov_gw_agent import NAME, _seeing, _tick

pytestmark = pytest.mark.unit
__all__ = ["clock", "gw", "host"]


def _said(acts) -> list[dict]:
    return [a[2] for a in acts if a[0] == "guest" and a[1] == "qview"]


def test_a_session_reopened_between_ticks_gets_the_current_view(gw, clock):
    _seeing(gw, clock)
    gw._quo_reserve_online, gw._quo_guest_session = True, 1
    first = _said(_tick(gw, clock))
    assert len(first) == 1 and first[0]["w"] == "see", "подготовка: первая сессия без взгляда"
    assert not _said(_tick(gw, clock)), "взгляд повторяется в той же сессии"
    gw._quo_guest_session = 2                                 # обрыв и подъём за один такт: online снова True
    again = _said(_tick(gw, clock))
    assert len(again) == 1 and again[0]["w"] == "see" and again[0]["tgt"] == "h", \
        "новая сессия между тактами не замечена — резерв держит прежний взгляд шлюза"
    assert not _said(_tick(gw, clock))


def test_a_session_number_seen_while_offline_is_said_once_online(gw, clock):
    _seeing(gw, clock)
    gw._quo_reserve_online, gw._quo_guest_session = False, 5
    assert not _said(_tick(gw, clock)), "взгляд ушёл без сессии"
    gw._quo_reserve_online = True
    assert len(_said(_tick(gw, clock))) == 1
    assert gw._gw_qvoters[NAME].view == "see"
