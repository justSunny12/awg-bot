"""Пакет awgbot.infra.routing отдаёт имена частей через __getattr__: подмена
на пакете (monkeypatch.setattr(routing, "X", …)) в pytest до 9.2 при откате
оставляла в нём значение, бывшее при подмене. Так плавал
test_routing_domains::test_external_ip_comes_from_the_link_peer_endpoint
(FIXES-uupd-3); сторож — tests/conftest.py, _routing_package_not_shadowed.
С pytest 9.2 откат снимает атрибут, которого в пакете не было, — утечки
через monkeypatch больше нет; сторож остаётся на прямое присваивание.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_a_package_level_patch_is_undone_and_the_part_keeps_the_name():
    """Подмена на пакете после отката не оставляет в нём имени части: пакет снова
    отдаёт то, что лежит в части (здесь — заглушка fake_routing на routing.probes).
    Подменять всё равно — на части, где имя определено."""
    from awgbot.infra import routing
    stub = lambda iface="": None                                  # noqa: E731 — как у fake_routing
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(routing.probes, "link_peer_endpoint", stub)   # fake_routing
        with pytest.MonkeyPatch.context() as inner:
            inner.setattr(routing, "link_peer_endpoint", lambda iface="": "203.0.113.50")
            assert routing.link_peer_endpoint(iface="x") == "203.0.113.50"
        try:
            assert "link_peer_endpoint" not in vars(routing), "подмена на пакете протекла — сторож conftest её снимет"
            assert routing.link_peer_endpoint is stub, "пакет отдаёт не то, что лежит в части"
        finally:
            vars(routing).pop("link_peer_endpoint", None)
    assert routing.link_peer_endpoint is routing.probes.link_peer_endpoint
