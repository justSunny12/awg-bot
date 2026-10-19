"""Пакет awgbot.infra.routing отдаёт имена частей через __getattr__: подмена
на пакете при откате оставляет в нём значение, бывшее при подмене. Так
плавал test_routing_domains::test_external_ip_comes_from_the_link_peer_endpoint
(FIXES-uupd-3); сторож — tests/conftest.py, _routing_package_not_shadowed.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_a_package_level_patch_leaks_and_the_guard_names_it():
    """Корень плавающего test_external_ip_comes_from_the_link_peer_endpoint:
    monkeypatch на пакете routing (имя части отдаёт __getattr__) при откате
    оставляет в пакете то значение, что было при подмене, — заглушку
    fake_routing. Сторож tests/conftest.py ловит такую утечку; подменять —
    на части (routing.probes)."""
    from awgbot.infra import routing
    stub = lambda iface="": None                                  # noqa: E731 — как у fake_routing
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(routing.probes, "link_peer_endpoint", stub)   # fake_routing
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr(routing, "link_peer_endpoint", lambda iface="": "203.0.113.50")
        leaked = vars(routing).get("link_peer_endpoint")
    try:
        assert leaked is stub, "механизм утечки не тот — пересмотри сторож"
        assert routing.link_peer_endpoint is stub, "пакет отдаёт заглушку вместо настоящей"
    finally:
        vars(routing).pop("link_peer_endpoint", None)
    assert routing.link_peer_endpoint is routing.probes.link_peer_endpoint
