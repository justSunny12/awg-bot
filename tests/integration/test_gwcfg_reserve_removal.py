"""Снятие раздела reserve у шлюза документом (domain/services/gwchannel/cfg.py:
gwcfg_diff): у сервера пары больше нет, а на шлюзе раздел стоит — документ
без раздела reserve едет, и агент снимает линк к резерву; у сервера раздела
нет и у шлюза нет — документ из-за резерва не едет.

Цена ошибки: после удаления пары линк шлюза к прежнему резерву живёт
вечно — постоянный туннель к чужому серверу с долей ключа переезда на
диске шлюза; документ «без резерва» каждому шлюзу без резерва — лишний
прогон применения на каждом такте.
"""
from __future__ import annotations

import pytest

from tests.integration import test_gwcfg_delivery as _d

pytestmark = pytest.mark.integration
host, slot = _d.host, _d.slot
__all__ = ["host", "slot"]


def _caps(services, *caps) -> None:
    services.gwlink_session_opened(1, "4.0.0", 2, list(caps or ("cfg1", "confirm1", "rsv1")))


def test_a_reserve_section_left_on_the_gateway_without_a_pair_is_due(services, slot):
    _caps(services)
    doc = services.gwcfg_doc(slot)
    assert not doc["prints"].get("reserve"), "у сервера без пары раздел reserve в документе"
    assert services.gwcfg_diff(slot, doc, _d._have(services, reserve="DUMMY-print-of-the-old-section")) == ["reserve"], \
        "раздел reserve на шлюзе без пары не снимается"


def test_no_reserve_on_either_side_brings_no_document(services, slot):
    _caps(services)
    doc = services.gwcfg_doc(slot)
    assert services.gwcfg_diff(slot, doc, _d._have(services, reserve="")) == []
    assert services.gwcfg_diff(slot, doc, _d._have(services)) == []


def test_the_document_without_reserve_reaches_the_gateway_after_its_snapshot(services, slot):
    _caps(services)
    _d._snap(services, _d._have(services, reserve="DUMMY-print-of-the-old-section"))
    body = services.gwcfg_due(slot)
    assert body is not None, "документ без раздела reserve шлюзу с разделом не ушёл"
    assert "reserve" not in body, "в документе для снятия раздела — сам раздел"
