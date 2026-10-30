"""Порт ответчика второй линии (domain/services/reserve/quorum/members.py, probes.py):
`st.quo` несёт порт моего ответчика, мой зонд к её AWG бьёт в её порт, зонд к своему
клону — в свой.

Мир — линия «Alpha» с зондами (`quo`, tests/integration/_reserve_quorum_common.py).

Цена ошибки: у серверов разные `app.reserve.quorum.port` — зонд к второй линии стучит
в свой порт, ответа нет, кандидат «не видит» живой сервер и голосует за переезд.
"""
from __future__ import annotations

import pytest

from awgbot.infra import quoprobe
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_quorum_common as qc
from tests.integration.test_reserve_quorum_probes import _other_side_conf, _ready

pytestmark = pytest.mark.integration
rsv = rc.rsv
quo = qc.quo


def _peer_line(s, **extra) -> None:
    s.rsv_update("rsv_peer_state", quo={"l": "b", "g": 2, "members": ["b", "a", "b1"], "active": True, "why": [], **extra})


def test_st_carries_the_port_of_my_responder(quo, monkeypatch):
    qc.enable(quo)
    monkeypatch.setattr(quoprobe, "port", lambda: 9911)
    assert quo.reserve_quorum_st_part()["port"] == 9911, "порт моего ответчика не едет второй стороне"


def test_my_probe_to_the_other_line_uses_her_responder_port(quo):
    _ready(quo)
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _other_side_conf()})
    _peer_line(quo, port=9911)
    want = quo.reserve_quorum_probe_ifaces()
    assert want["awgqbh"]["port"] == 9911, f"зонд ко второй линии — не в её порт: {want['awgqbh']}"


def test_without_her_port_the_probe_uses_mine(quo):
    """Вторая сторона до правки: поля нет — прежнее поведение, свой порт."""
    _ready(quo)
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _other_side_conf()})
    _peer_line(quo)
    assert quo.reserve_quorum_probe_ifaces()["awgqbh"]["port"] == quoprobe.port()


def test_the_probe_to_my_own_clone_keeps_my_port(quo):
    _ready(quo)
    quo.reserve_quorum_pair_in("qprobe", {"l": "b", "g": 2, "conf": _other_side_conf()})
    _peer_line(quo, port=9911)
    quo.rsv_update("rsv_out", state="away")
    want = quo.reserve_quorum_probe_ifaces()
    assert want["awgqar"]["port"] == quoprobe.port(), f"зонд к своему клону — в порт второй линии: {want['awgqar']}"
    assert want["awgqbh"]["port"] == 9911
