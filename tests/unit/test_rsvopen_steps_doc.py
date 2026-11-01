"""Шаги процесса открытия резерва (tools/rsvopen.py) и их описание в
docs/README-bot.md: поля, которые документ обещает у шага `journal` и `store`,
— те, что процесс пишет.

Цена ошибки: админ (и тот, кто разбирает срыв переезда по
`/run/awg-bot/rsv/open.status`) ищет у шага `journal` поля `epoch`, `gap`,
`lost`, которых там нет, и решает, что разрыва журнала не было, — а разрыв
стоит полями `jgap`/`jlost` в `clone_up` и `done`.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _steps() -> dict[str, list[ast.Call]]:
    tree = ast.parse((ROOT / "tools" / "rsvopen.py").read_text())
    out: dict[str, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_step"
                and node.args and isinstance(node.args[0], ast.Constant)):
            out.setdefault(str(node.args[0].value), []).append(node)
    return out


def _kw(calls: list[ast.Call]) -> set[str]:
    return {k.arg or "**" + ast.unparse(k.value) for c in calls for k in c.keywords}


def test_journal_step_fields_in_the_readme_match_the_process():
    steps = _steps()
    assert _kw(steps["journal"]) == set(), "у шага journal появились поля — опиши их в README-bot"
    assert _kw(steps["store"]) == {"from_snapshot"}
    assert "**jinfo" in _kw(steps["clone_up"]) and "**jinfo" in _kw(steps["done"])
    src = (ROOT / "tools" / "rsvopen.py").read_text()
    assert re.search(r'return \{"jgap": .*"jlost": ', src), "разрыв журнала ушёл из jinfo"
    doc = " ".join((ROOT / "docs" / "README-bot.md").read_text().split())
    part = doc[doc.index("→ `journal` (только снимок журнала"):doc.index("→ `clone_up` (`--take`")]
    for gone in ("`epoch`", "`role`", "`fork`", "`events`", "`guest_events`", "`counts`", "`gap`", "`lost`"):
        assert gone not in part, f"README обещает у шага journal поле {gone}, которого rsvopen не пишет"
    assert "`jgap`" in part and "`jlost`" in part and "`clone_up`" in part and "`done`" in part
    assert "`from_snapshot`" in part
