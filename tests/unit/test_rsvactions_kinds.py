"""Виды действий автоматов резерва: домен отдаёт строковые кортежи
(`("send", вид, тело)`, `("fw",)`, …), исполнитель (runtime/rsvactions) знает
набор ACTION_KINDS. Сторож сверяет первые элементы кортежей в списках и
`.append(...)` пакета awgbot/domain/services/reserve с этим набором; неизвестный
вид исполнитель пропускает с предупреждением в журнал.

Цена ошибки: опечатка в виде действия («sned») — действие молча не исполнено
(раньше строкой log.info среди сотен таких же): слово второй стороне не ушло,
файервол не перерисован, поллинг не взят — и ни один тест этого не видит.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from awgbot.runtime import rsvactions

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
RESERVE = ROOT / "awgbot" / "domain" / "services" / "reserve"
# Кортежи того же вида, но не действия этого исполнителя: пробы кворума
# (исполняет runtime/quorum до передачи остального сюда) и слова журнала гостя
# (их отправляет вызывающий как ("send", вид, тело)).
NOT_ACTIONS = {"probe", "jev", "jcnt", "jneed"}


def _kinds() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in RESERVE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            cands = []
            if isinstance(n, ast.List):
                cands = n.elts
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "append" and n.args:
                cands = [n.args[0]]
            for e in cands:
                if isinstance(e, ast.Tuple) and e.elts and isinstance(e.elts[0], ast.Constant) \
                        and isinstance(e.elts[0].value, str):
                    out.setdefault(e.elts[0].value, set()).add(str(path.relative_to(ROOT)))
    return out


def test_every_action_kind_of_the_domain_is_known_to_the_executor():
    kinds = _kinds()
    unknown = {k: sorted(v) for k, v in kinds.items() if k not in rsvactions.ACTION_KINDS | NOT_ACTIONS}
    assert not unknown, f"виды действий, которых исполнитель не знает: {unknown}"
    assert {"send", "notify", "fw", "live"} <= set(kinds), "сторож не нашёл действий домена — разбор сломан"


def test_every_known_kind_has_a_branch_in_the_executor():
    """Набор и ветки _one — одно и то же: вид в наборе без ветки снова тихо пропадал бы."""
    src = (ROOT / "awgbot" / "runtime" / "rsvactions.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_one")
    branches = {c.comparators[0].value for c in ast.walk(fn)
                if isinstance(c, ast.Compare) and isinstance(c.left, ast.Name) and c.left.id == "kind"
                and isinstance(c.comparators[0], ast.Constant)}
    assert branches == set(rsvactions.ACTION_KINDS)


async def test_an_unknown_kind_is_skipped_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=rsvactions.log.name):
        await rsvactions.run([("sned", "qprobe", {})])
    assert any("неизвестное действие 'sned'" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records), caplog.text
