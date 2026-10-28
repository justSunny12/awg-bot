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


_DEF = re.compile(r"^(?:async def |def |class )([A-Za-z_]\w*)\b|^([A-Za-z_]\w*)\s*(?::[^=\n]*)?=", re.M)


def _definitions(src: str) -> Counter:
    """Определения верхнего уровня файла по именам (def, class, присваивание
    с начала строки) — одним проходом на файл: ссылкой они не считаются."""
    return Counter(m.group(1) or m.group(2) for m in _DEF.finditer(src))


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


def _modules(bot: pathlib.Path) -> list[pathlib.Path]:
    return [*sorted((bot / "texts").rglob("*.py")), *sorted((bot / "keyboards").glob("*.py")),
            *sorted((bot / "sections").glob("*.py"))]


def dead_names(pkg_root: pathlib.Path = PKG) -> list[str]:
    """pkg_root — каталог пакета awgbot (сторож сторожа подставляет свой)."""
    bot = pkg_root / "bot"
    code = {p: _ALL.sub("", p.read_text(encoding="utf-8")) for p in pkg_root.rglob("*.py")}
    # ссылки файла — его идентификаторы без его же определений
    refs_in = {p: _identifiers(src) - _definitions(src) for p, src in code.items()}
    counts = {p: _identifiers(src) for p, src in code.items()}
    readers_of: dict[str, list[pathlib.Path]] = {}
    out = []
    for mod in _modules(bot):
        if mod.name == "__init__.py":
            continue
        pkg = mod.relative_to(bot).parts[0]                   # texts | keyboards | sections
        reexport = mod.parent / "__init__.py" if pkg != "sections" else None
        # считают только файлы, которым пакет виден: сам пакет и те, кто его
        # импортирует; одноимённый обработчик или метод сервиса — не ссылка
        if pkg not in readers_of:
            word = re.compile(r"\b" + pkg + r"\b")
            readers_of[pkg] = [p for p, src in code.items() if p.is_relative_to(bot / pkg) or word.search(src)]
        readers = readers_of[pkg]
        for name in sorted(_public_names(code[mod])):
            refs = sum(refs_in[p][name] for p in readers)
            if reexport is not None:
                refs -= counts[reexport][name]
                outer = reexport.parent.parent / "__init__.py"          # texts/__init__ над texts/routing
                if outer.exists() and outer in counts:
                    refs -= counts[outer][name]
            if refs == 0:
                out.append(f"{mod.relative_to(bot)}:{name}")
    return out


def test_every_public_name_of_texts_keyboards_and_sections_is_used():
    assert dead_names() == [], "имена без ссылок в awgbot/ — снять вместе с тестами на них"


def _tree(root: pathlib.Path, files: dict[str, str]) -> pathlib.Path:
    for rel, src in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
    return root


def test_the_guard_notices_a_fresh_dead_name(tmp_path):
    """Сторож сторожа на своём пакете: имя, на которое ссылается только тест,
    докстринг или реэкспорт, — мёртвое; и в подпакете routing тоже. Имя,
    которое зовёт обработчик, — живое. Иначе зелёный сторож ничего не значит."""
    pkg = _tree(tmp_path / "awgbot", {
        "__init__.py": "",
        "bot/__init__.py": "",
        "bot/texts/__init__.py": "from .fmt import USED, ONLY_IN_TESTS\n",
        "bot/texts/fmt.py": "USED = 1\nONLY_IN_TESTS = 2\n",
        "bot/texts/routing/__init__.py": "from .slots import DEAD_IN_ROUTING, LIVE_IN_ROUTING\n",
        "bot/texts/routing/slots.py": "DEAD_IN_ROUTING = 1\nLIVE_IN_ROUTING = 2\n",
        "bot/keyboards/__init__.py": "",
        "bot/sections/__init__.py": "",
        "bot/handlers/__init__.py": "",
        "bot/handlers/main.py": ('"""Упоминание ONLY_IN_TESTS в докстринге — не ссылка."""\n'
                                 "from awgbot.bot import texts\n\n\n"
                                 "def show():\n    return texts.USED, texts.routing.LIVE_IN_ROUTING\n"),
    })
    _tree(tmp_path, {"tests/test_fmt.py": "from awgbot.bot.texts import ONLY_IN_TESTS\n"})
    assert dead_names(pkg) == ["texts/fmt.py:ONLY_IN_TESTS", "texts/routing/slots.py:DEAD_IN_ROUTING"], \
        "имя только из теста, докстринга и реэкспорта — мёртвое, живое из обработчика — нет"
