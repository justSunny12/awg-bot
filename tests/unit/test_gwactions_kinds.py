"""Виды действий агента шлюза о резерве и кворуме: домен (awgbot/domain/gateway/reserve)
отдаёт строковые кортежи (`("guest", вид, тело)`, `("push",)`, `("tg", "on")`, …),
исполнитель — runtime/guestlink.run_actions (`_one`), пробы кворума до него снимает
runtime/quorum. Сторож сверяет первые элементы кортежей в списках, генераторах списков
и `.append(...)` с ветками исполнителя; неизвестный вид исполнитель пропускает с
предупреждением в журнал.

Цена ошибки: опечатка в виде действия — слово резерву не ушло, пересылка не открыта,
уведомление Т-РА5 не отправлено, и ни один тест этого не видит (раньше у исполнителя
шлюза сторожа не было, а генераторы списков не разбирал и сторож резерва).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from awgbot.runtime import guestlink

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GW_RESERVE = ROOT / "awgbot" / "domain" / "gateway" / "reserve"
# Не действия этого исполнителя: пробы голосующего (их шлёт runtime/quorum до передачи остального
# сюда) и проба основы по каналу конфигурации (её исполняет такт наблюдателей сам).
NOT_ACTIONS = {"probe", "gw_ping"}


def action_kinds(root: Path) -> dict[str, set[str]]:
    """Вид действия → файлы: кортежи с первым строковым элементом в списках, генераторах
    списков (`[("gw", s, …) for …]`) и `.append(...)`."""
    out: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            cands = []
            if isinstance(n, ast.List):
                cands = n.elts
            elif isinstance(n, (ast.ListComp, ast.GeneratorExp)):
                cands = [n.elt]
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "append" and n.args:
                cands = [n.args[0]]
            for e in cands:
                if isinstance(e, ast.Tuple) and e.elts and isinstance(e.elts[0], ast.Constant) \
                        and isinstance(e.elts[0].value, str):
                    out.setdefault(e.elts[0].value, set()).add(str(path.relative_to(ROOT)))
    return out


def executor_branches() -> set[str]:
    src = (ROOT / "awgbot" / "runtime" / "guestlink.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_one")
    return {c.comparators[0].value for c in ast.walk(fn)
            if isinstance(c, ast.Compare) and isinstance(c.left, ast.Name) and c.left.id == "kind"
            and isinstance(c.comparators[0], ast.Constant)}


def test_every_action_kind_of_the_gateway_domain_has_a_branch_in_the_executor():
    kinds = action_kinds(GW_RESERVE)
    branches = executor_branches()
    unknown = {k: sorted(v) for k, v in kinds.items() if k not in branches | NOT_ACTIONS}
    assert not unknown, f"виды действий агента, которых исполнитель гостевого канала не знает: {unknown}"
    assert {"guest", "push", "notify", "open", "close", "tg", "reresolve"} <= set(kinds), \
        "сторож не нашёл действий агента — разбор сломан"


def test_the_guard_reads_list_comprehensions():
    """Генератор списка — такой же источник действий, как список: без его разбора
    `[("gw", s, "state", body) for s in …]` остался бы вне сторожа."""
    tree = ast.parse('def f(xs):\n    return [("sned", x) for x in xs] + list(("push",) for x in xs)\n')
    kinds = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.ListComp, ast.GeneratorExp)) and isinstance(n.elt, ast.Tuple):
            kinds.add(n.elt.elts[0].value)
    assert kinds == {"sned", "push"}
    assert "push" in action_kinds(GW_RESERVE) and "notify" in action_kinds(GW_RESERVE)


async def test_an_unknown_kind_is_skipped_without_breaking_the_rest(caplog):
    import logging
    done = []

    async def one(services, act):
        if act[0] == "sned":
            raise RuntimeError("неизвестное действие")
        done.append(act[0])
    real = guestlink._one
    guestlink._one = one
    try:
        with caplog.at_level(logging.WARNING, logger=guestlink.log.name):
            await guestlink.run_actions(None, [("sned", "x"), ("push",)])
    finally:
        guestlink._one = real
    assert done == ["push"] and any("sned" in r.getMessage() for r in caplog.records)
