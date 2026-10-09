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
MODULES = [*sorted((BOT / "texts").rglob("*.py")), *sorted((BOT / "keyboards").glob("*.py")),
           *sorted((BOT / "sections").glob("*.py"))]
_ALL = re.compile(r"__all__\s*=\s*\[[^\]]*\]", re.S)
_IDENT = re.compile(r"[A-Za-z_]\w*")


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


def _identifiers(src: str) -> Counter:
    """Идентификаторы кода: имена, атрибуты, импорты и строки (ключи getattr);
    докстринги и комментарии — не ссылки."""
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
    out = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out[node.id] += 1
        elif isinstance(node, ast.Attribute):
            out[node.attr] += 1
        elif isinstance(node, ast.alias):
            out[node.name.rsplit(".", 1)[-1]] += 1
            if node.asname:
                out[node.asname] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] += 1
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out.update(_IDENT.findall(node.value))
    return out


def dead_names(extra: dict | None = None) -> list[str]:
    """extra — {модуль: текст}, подменяет исходник (для проверки сторожа)."""
    code = {p: _ALL.sub("", (extra or {}).get(p) or p.read_text(encoding="utf-8")) for p in PKG.rglob("*.py")}
    counts = {p: _identifiers(src) for p, src in code.items()}
    out = []
    for mod in MODULES:
        if mod.name == "__init__.py":
            continue
        pkg = mod.relative_to(BOT).parts[0]                   # texts | keyboards | sections
        reexport = mod.parent / "__init__.py" if pkg != "sections" else None
        # считают только файлы, которым пакет виден: сам пакет и те, кто его
        # импортирует; одноимённый обработчик или метод сервиса — не ссылка
        readers = [p for p, src in code.items()
                   if p.is_relative_to(BOT / pkg) or re.search(r"\b" + pkg + r"\b", src)]
        for name in sorted(_public_names(code[mod])):
            refs = sum(counts[p][name] - _definitions(code[p], name) for p in readers)
            if reexport is not None:
                refs -= counts[reexport][name]
                outer = reexport.parent.parent / "__init__.py"          # texts/__init__ над texts/routing
                if outer.exists() and outer in counts:
                    refs -= counts[outer][name]
            if refs == 0:
                out.append(f"{mod.relative_to(BOT)}:{name}")
    return out


def test_every_public_name_of_texts_keyboards_and_sections_is_used():
    assert dead_names() == [], "имена без ссылок в awgbot/ — снять вместе с тестами на них"


def test_the_guard_notices_a_fresh_dead_name():
    """Сторож сторожа: имя, на которое ссылается только тест, докстринг или
    реэкспорт, — мёртвое; и в подпакете routing тоже."""
    fmt = BOT / "texts" / "fmt.py"
    src = fmt.read_text(encoding="utf-8") + "\nONLY_IN_TESTS = 1\n"
    assert dead_names({fmt: src}) == ["texts/fmt.py:ONLY_IN_TESTS"]
    init = BOT / "texts" / "__init__.py"
    reexported = init.read_text(encoding="utf-8") + "\nfrom .fmt import ONLY_IN_TESTS\n"
    handler = BOT / "handlers" / "client.py"
    doc = '"""Упоминание ONLY_IN_TESTS в докстринге — не ссылка."""\n' + handler.read_text(encoding="utf-8")
    assert dead_names({fmt: src, init: reexported, handler: doc}) == ["texts/fmt.py:ONLY_IN_TESTS"]
    slots = BOT / "texts" / "routing" / "slots.py"
    rinit = BOT / "texts" / "routing" / "__init__.py"
    assert dead_names({slots: slots.read_text(encoding="utf-8") + "\nDEAD_IN_ROUTING = 1\n",
                       rinit: rinit.read_text(encoding="utf-8") + "\nfrom .slots import DEAD_IN_ROUTING\n"}) \
        == ["texts/routing/slots.py:DEAD_IN_ROUTING"]
