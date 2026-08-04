"""
Ловля висячих ссылок на модульные константы.

Почему отдельным тестом. Рефакторинг удалил из infra/routing.py константу _RULE,
а две ветки рубильника продолжали на неё ссылаться. Python такое не ловит при
импорте — только в момент вызова, и там это NameError, который не является
RoutingError, а значит проходил мимо прицельного `except` и оседал общим
warning'ом в планировщике. Итог: аварийное снятие маркировки не срабатывало
НИ РАЗУ, и при отказе шлюза трафик продолжал уходить в нерабочий тоннель.

Обычные тесты этого не видели, потому что подменяли всю функцию целиком —
фейк не ссылается на константы настоящего модуля.

Проверяем узкий, но точный признак: ссылка на приватную КОНСТАНТУ модуля
(_ЗАГЛАВНЫМИ), которой в модуле не присвоено. Локальных переменных в таком
регистре не пишут, поэтому ложных срабатываний тут практически нет, а класс
ошибки закрывается целиком.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "awgbot"
CONST = re.compile(r"^_[A-Z][A-Z0-9_]*$")


def _modules() -> list[Path]:
    return sorted(ROOT.rglob("*.py"))


def _assigned_names(tree: ast.AST) -> set[str]:
    """Все имена, которым в модуле что-то присваивается или которые импортируются
    (на любом уровне вложенности — важно лишь, что имя в модуле существует)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                found.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Global):
            found.update(node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
    return found


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_module_constants_are_defined(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assigned = _assigned_names(tree)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and CONST.match(n.id)}
    dangling = sorted(used - assigned)
    assert not dangling, (
        f"{path.relative_to(ROOT.parent)}: ссылки на несуществующие константы "
        f"{dangling}. Такое падает NameError только в момент вызова — то есть "
        f"на боевом сервере.")
