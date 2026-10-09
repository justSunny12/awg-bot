"""Unit: мёртвые имена в текстах, клавиатурах и разделах.

Публичное имя модуля texts/*, keyboards/*, sections/*, на которое в awgbot/
никто не ссылается (своё определение и реэкспорт в texts/__init__ и
keyboards/__init__ не в счёт), — мёртвое. Ссылка только из тестов тоже не
считается: такое имя держит тесты, а не интерфейс. Проверяется по именам
в коде, включая строки (ключи getattr и __all__ модуля сюда не входят).
"""

from __future__ import annotations

import ast
import pathlib
import re
from collections import Counter

from awgbot.bot import keyboards as kbm

BOT = pathlib.Path(kbm.__file__).parent.parent              # awgbot/bot
PKG = BOT.parent                                             # awgbot/
REEXPORTS = {BOT / "texts" / "__init__.py", BOT / "keyboards" / "__init__.py"}
MODULES = [*sorted((BOT / "texts").rglob("*.py")), *sorted((BOT / "keyboards").glob("*.py")),
           *sorted((BOT / "sections").glob("*.py"))]
_ALL = re.compile(r"__all__\s*=\s*\[[^\]]*\]", re.S)


def _public_names(src: str) -> set[str]:
    out = set()
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return {n for n in out if not n.startswith("__")}


def _definitions(src: str, name: str) -> int:
    return len(re.findall(r"^(?:async def |def |class )" + re.escape(name) + r"\b|^"
                          + re.escape(name) + r"\s*(?::[^=\n]*)?=", src, re.M))


_IDENT = re.compile(r"[A-Za-z_]\w*")


def dead_names(extra: dict | None = None) -> list[str]:
    """extra — {модуль: текст}, подменяет исходник (для проверки сторожа).
    Счёт — по идентификаторам в тексте (и внутри строк: ключи getattr)."""
    code = {p: _ALL.sub("", (extra or {}).get(p) or p.read_text(encoding="utf-8")) for p in PKG.rglob("*.py")}
    counts = {p: Counter(_IDENT.findall(src)) for p, src in code.items()}
    total = Counter()
    for c in counts.values():
        total.update(c)
    out = []
    for mod in MODULES:
        if mod.name == "__init__.py":
            continue
        reexport = next((p for p in REEXPORTS if p.parent == mod.parent), None)
        for name in sorted(_public_names(code[mod])):
            refs = total[name] - _definitions(code[mod], name)
            if reexport is not None:
                refs -= counts[reexport][name]
            if refs == 0:
                out.append(f"{mod.relative_to(BOT)}:{name}")
    return out


def test_every_public_name_of_texts_keyboards_and_sections_is_used():
    assert dead_names() == [], "имена без ссылок в awgbot/ — снять вместе с тестами на них"


def test_the_guard_notices_a_fresh_dead_name():
    """Сторож сторожа: имя, на которое ссылается только тест, — мёртвое."""
    mod = BOT / "texts" / "fmt.py"
    src = mod.read_text(encoding="utf-8") + "\nONLY_IN_TESTS = 1\n"
    assert dead_names({mod: src}) == ["texts/fmt.py:ONLY_IN_TESTS"]
