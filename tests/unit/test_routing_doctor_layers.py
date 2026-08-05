"""
Доктор обязан не только находить отказ, но и указывать на нужный слой.

Ошибка адресации здесь дороже ложного срабатывания: человек идёт чинить не ту
машину. Так и вышло в бою — линк молчал три с половиной часа, а слой докладывал
«Линк поднят» зелёным, и внимание уезжало на шлюз.
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.infra import routing
from awgbot.runtime import routing_doctor as doc

pytestmark = pytest.mark.unit


@pytest.fixture
def healthy(monkeypatch):
    """Все слои зелёные — чтобы тест менял ровно одну переменную."""
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    monkeypatch.setattr(routing, "self_check", lambda force=False: (True, "ок"))
    monkeypatch.setattr(routing, "link_peer_address", lambda: "10.99.99.2")
    monkeypatch.setattr(routing, "table_route", lambda: "default dev awglink")
    monkeypatch.setattr(routing, "rule_present", lambda: True)
    monkeypatch.setattr(routing, "mss_clamp_present", lambda: True)
    monkeypatch.setattr(routing, "probe_gateway", lambda *a, **k: routing.PROBE_OK)
    monkeypatch.setattr(routing, "last_probe_latency_ms", lambda: 40)
    monkeypatch.setattr(routing, "hook_present", lambda: True)
    monkeypatch.setattr(routing, "list_sets", lambda: [])


def _link_row(rows):
    return [r for r in rows if "Линк" in r[1]][0]


def test_fresh_handshake_is_green(healthy, monkeypatch):
    monkeypatch.setattr(routing, "link_handshake_age", lambda: 42)
    assert _link_row(doc._probe_layers())[0] == doc._OK


def test_stale_handshake_is_a_failure_not_a_green_line(healthy, monkeypatch):
    """Хендшейк в три часа при пересогласовании раз в две минуты — мёртвый линк.

    Прежде слой красил зелёным всё, где хендшейк просто существует, и человек
    шёл чинить шлюз вместо стороны, которая молчит.
    """
    monkeypatch.setattr(routing, "link_handshake_age", lambda: 12551)
    row = _link_row(doc._probe_layers())
    assert row[0] == doc._BAD, "протухший хендшейк обязан быть отказом"
    assert "шлюз" in row[2].lower(), "нужно назвать, где чинить"


def test_short_blip_does_not_redden_a_working_link(healthy, monkeypatch):
    """Порог с запасом: короткий провал связи не должен краснить исправный линк."""
    monkeypatch.setattr(routing, "link_handshake_age", lambda: 200)
    assert _link_row(doc._probe_layers())[0] == doc._OK


def test_threshold_exceeds_wireguard_reject_time():
    """Порог обязан быть больше REJECT_AFTER_TIME, иначе ловил бы норму."""
    assert doc._STALE_HANDSHAKE > 180
