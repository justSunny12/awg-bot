"""Unit: нижние слои не импортируют слой бота.

domain, infra, core и util не тянут awgbot.bot — ни на верхнем уровне, ни
лениво внутри функций, ни относительным импортом, ни строкой для
import_module. Тексты уведомлений домена живут в domain/notices, помощники
форматирования — в util/textfmt; клавиатуру домен называет по имени
(Notification.keyboard), собирает её бот.

Цена ошибки: ленивый импорт бота из домена не виден при загрузке модуля —
агент шлюза и CLI без aiogram падают в момент вызова, а слой бота
незаметно становится зависимостью домена (и его тестов).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

PKG = pathlib.Path(__file__).resolve().parents[2] / "awgbot"
LAYERS = ("domain", "infra", "core", "util")
BOT = "awgbot.bot"


def _module_name(path: pathlib.Path) -> str:
    parts = list(path.relative_to(PKG.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_bot(name: str) -> bool:
    return name == BOT or name.startswith(BOT + ".")


def bot_imports(path: pathlib.Path, src: str | None = None) -> list[str]:
    """«строка: модуль» каждого импорта слоя бота в файле — на любом уровне
    вложенности; относительные разрешаются от пакета файла."""
    src = path.read_text(encoding="utf-8") if src is None else src
    mod = _module_name(path)
    pkg = mod if path.name == "__init__.py" else mod.rpartition(".")[0]
    out = []
    for node in ast.walk(ast.parse(src)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                head = ".".join(base + ([node.module] if node.module else []))
            else:
                head = node.module or ""
            names = [head] + [f"{head}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names = [node.value]                 # import_module("awgbot.bot…"), пути подмены
        if any(_is_bot(n) for n in names):
            out.append(f"{node.lineno}: {next(n for n in names if _is_bot(n))}")
    return out


def _files():
    for layer in LAYERS:
        yield from sorted((PKG / layer).rglob("*.py"))


def test_lower_layers_do_not_import_the_bot():
    bad = [f"{p.relative_to(PKG.parent)}:{hit}" for p in _files() for hit in bot_imports(p)]
    assert not bad, "нижний слой тянет слой бота:\n" + "\n".join(bad)


def test_the_scan_sees_lazy_relative_and_string_imports():
    """Сторож сторожа: ленивый импорт внутри функции, относительный и строкой
    — всё находится; упоминание в докстринге и соседний пакет — нет."""
    probe = PKG / "domain" / "services" / "probe.py"
    src = ('"""Тексты раньше брались из awgbot.bot.texts."""\n'
           "def f():\n    from awgbot.bot import texts\n    return texts\n"
           "def g():\n    from ...bot.keyboards import x\n    return x\n"
           "def h():\n    import importlib\n    return importlib.import_module('awgbot.bot.ui')\n"
           "def k():\n    from awgbot.botany import leaf\n    from ..notices import routing\n"
           "    return leaf, routing\n")
    hits = bot_imports(probe, src)
    assert [h.split(": ")[1] for h in hits] == ["awgbot.bot", "awgbot.bot.keyboards", "awgbot.bot.ui"], hits
    assert {p.parts[-2] for p in _files() if p.name == "__init__.py"} >= {"notices", "textfmt"}


@pytest.mark.parametrize("mod", sorted(p.stem for p in (PKG / "domain" / "notices").glob("*.py")
                                       if p.stem != "__init__"))
def test_each_notice_module_loads_first_and_alone(mod):
    """Каждую часть notices можно загрузить первой в чистом процессе: без
    встречного цикла с сервисами и без слоя бота и aiogram в sys.modules."""
    import subprocess
    import sys
    code = (f"import sys, awgbot.domain.notices.{mod}\n"
            "print(sorted(m for m in sys.modules if m.startswith(('awgbot.bot', 'aiogram'))))")
    r = subprocess.run([sys.executable, "-c", code], cwd=PKG.parent, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]", r.stdout
