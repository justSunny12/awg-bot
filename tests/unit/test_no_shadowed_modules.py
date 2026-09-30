"""
Ловля затенения модуля локальным именем.

Почему отдельным тестом. После разбиения infra/routing.py на пакет функция
ensure_policy(active_iface, slots=()) звала соседа `slots.ensure_slot_policy`,
а параметр slots — список — затенил модуль: импорт соседа pyflakes счёл
лишним (имя-то определено) и он был снят, а вызов остался. Python такое не
ловит при импорте, pyflakes молчит, падает это в момент вызова: такт живости
шлюзов валился каждые 30 с с «'list' object has no attribute
'ensure_slot_policy'», окно замеров не наполнялось, и резерв навсегда
«проверяется». Обычные тесты не видели: они подменяли ensure_policy целиком.

Признак узкий и точный: в функции есть локальное имя (параметр, присваивание,
переменная цикла, with/except), совпадающее с именем модуля — соседа по пакету
или импортированного, — и по этому имени в той же функции берут атрибут,
который в том модуле определён на верхнем уровне. Модули и их имена читаем
статически, без импорта кода.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "awgbot"


def _modules() -> list[Path]:
    return sorted(ROOT.rglob("*.py"))


def _module_file(base_dir: Path, name: str) -> Path | None:
    if (base_dir / f"{name}.py").is_file():
        return base_dir / f"{name}.py"
    if (base_dir / name / "__init__.py").is_file():
        return base_dir / name / "__init__.py"
    return None


@lru_cache(maxsize=None)
def _top_names(path: Path) -> frozenset[str]:
    """Имена верхнего уровня модуля: функции, классы, присваивания, импорты."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names if a.name != "*"}
    return frozenset(names)


def _module_names(path: Path, tree: ast.Module) -> dict[str, Path]:
    """Имя → файл модуля: соседи по пакету и то, что импортировано как модуль."""
    found: dict[str, Path] = {}
    for sib in path.parent.iterdir():
        if sib == path or sib.name.startswith("__"):
            continue
        f = _module_file(path.parent, sib.stem if sib.suffix == ".py" else sib.name)
        if f is not None and sib.suffix in (".py", ""):
            found[sib.stem if sib.suffix == ".py" else sib.name] = f
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] != "awgbot":
                    continue
                f = _module_file(ROOT.parent.joinpath(*parts[:-1]), parts[-1])
                if f is not None:
                    found[a.asname or parts[0]] = f if a.asname else _module_file(ROOT.parent, "awgbot")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                if node.module:
                    base = base.joinpath(*node.module.split("."))
            elif node.module and node.module.split(".")[0] == "awgbot":
                base = ROOT.parent.joinpath(*node.module.split("."))
            else:
                continue
            for a in node.names:
                f = _module_file(base, a.name) if a.name != "*" else None
                if f is not None:
                    found[a.asname or a.name] = f
    return found


def _locals(fn: ast.AST) -> set[str]:
    names = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    names |= {fn.args.vararg.arg} if fn.args.vararg else set()
    names |= {fn.args.kwarg.arg} if fn.args.kwarg else set()
    declared = {g for x in ast.walk(fn) if isinstance(x, (ast.Global, ast.Nonlocal)) for g in x.names}
    names |= {x.id for x in ast.walk(fn) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
    return names - declared


def _shadowing(path: Path, tree: ast.Module) -> list[str]:
    mods = _module_names(path, tree)
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        shadowed = _locals(fn) & set(mods)
        for x in ast.walk(fn):
            if (isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
                    and x.value.id in shadowed and x.attr in _top_names(mods[x.value.id])):
                bad.append(f"{fn.name}: {x.value.id}.{x.attr}")
    return sorted(set(bad))


@pytest.mark.parametrize("path", _modules(), ids=lambda p: p.name)
def test_no_local_name_shadows_a_module_it_uses(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = _shadowing(path, tree)
    assert not bad, (
        f"{path.relative_to(ROOT.parent)}: локальное имя затеняет модуль, а по нему берут имя из "
        f"этого модуля — {bad}. В рабочем коде это AttributeError в момент вызова; переименуй "
        f"локальное имя или импортируй модуль под псевдонимом.")


def test_the_guard_sees_the_original_slip(tmp_path):
    """Сторож самого теста: воспроизведение ошибки 3.1.0.19 обязан ловить —
    иначе зелёный прогон ничего не значит."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "slots.py").write_text("def ensure_slot_policy(sid, iface):\n    pass\n", encoding="utf-8")
    bad = pkg / "policy.py"
    bad.write_text("def ensure_policy(active_iface='', slots=()):\n"
                   "    for sid, iface, _n in slots:\n        slots.ensure_slot_policy(sid, iface)\n",
                   encoding="utf-8")
    tree = ast.parse(bad.read_text(encoding="utf-8"))
    assert _shadowing(bad, tree) == ["ensure_policy: slots.ensure_slot_policy"]
    ok = pkg / "sets.py"
    ok.write_text("def snapshot(names):\n    sets = {}\n    sets.setdefault('a', [])\n    return sets\n",
                  encoding="utf-8")
    assert _shadowing(ok, ast.parse(ok.read_text(encoding="utf-8"))) == []
