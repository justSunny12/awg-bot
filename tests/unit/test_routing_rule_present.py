"""Проверка главного правила маркировки РФ-доступа (infra/routing/marking
`_rule_present`): «стоит» — только строка `ip rule show` с нашей меткой (с
маской или без) и нашей таблицей, словами, а не подстроками по всему выводу.

Цена ошибки: при ≥ 4 слотах метка слота (0x10…) содержит «0x1», а правило
зоны или транзита (таблица 100, 1001…) — «lookup 100»; удалённое главное
правило считалось стоящим, такт его не восстанавливал — трафик пользователей
к российским сервисам шёл мимо шлюза.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config
from awgbot.infra.routing import base, marking, slots

pytestmark = pytest.mark.unit

MARK = f"0x{config.ROUTING_FWMARK:x}"
TABLE = config.ROUTING_TABLE


def _present(monkeypatch, text: str) -> bool:
    monkeypatch.setattr(base, "_host", lambda *a, **k: types.SimpleNamespace(stdout=text.encode(), returncode=0))
    return marking._rule_present()


def test_other_rules_with_lookalike_mark_and_table_are_not_ours(monkeypatch):
    text = ("0:\tfrom all lookup local\n"
            f"150:\tfrom all iif awggr-h lookup {TABLE}\n"
            f"151:\tfrom all fwmark 0x10 lookup {TABLE}1\n"
            f"32765:\tfrom all fwmark 0x{slots.slot_mark(4):x}/0x{slots.slot_mark(4):x} lookup {slots.slot_table(4)}\n"
            "32766:\tfrom all lookup main\n")
    assert _present(monkeypatch, text) is False


@pytest.mark.parametrize("rule", [f"fwmark {MARK}/{MARK} lookup {TABLE}", f"fwmark {MARK} lookup {TABLE}"])
def test_main_rule_found(monkeypatch, rule):
    text = f"0:\tfrom all lookup local\n32764:\tfrom all {rule}\n32766:\tfrom all lookup main\n"
    assert _present(monkeypatch, text) is True


@pytest.mark.parametrize("rule", [f"fwmark {MARK}/0xff lookup {TABLE}", f"fwmark {MARK}/{MARK} lookup {TABLE}0"])
def test_foreign_mask_or_table_is_not_ours(monkeypatch, rule):
    assert _present(monkeypatch, f"32764:\tfrom all {rule}\n") is False
