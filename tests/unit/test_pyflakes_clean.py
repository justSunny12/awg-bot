"""
pyflakes по коду поставки и тестам — чисто, в каждом прогоне pytest.

Почему тестом, а не только шагом выпуска. Рефакторинг удалил из
infra/routing.py константу _RULE, а две ветки рубильника продолжали на неё
ссылаться. Python такое не ловит при импорте — только в момент вызова, и там
это NameError, который не является RoutingError, а значит проходил мимо
прицельного `except` и оседал общим warning'ом в планировщике. Итог:
аварийное снятие маркировки не срабатывало НИ РАЗУ, и при отказе шлюза трафик
продолжал уходить в нерабочий тоннель. Обычные тесты этого не видели: они
подменяли всю функцию целиком, а фейк не ссылается на константы настоящего
модуля. Ловил это прежде узкий сторож «приватная константа без присваивания»;
pyflakes ловит тот же класс целиком (любое неопределённое имя) и заодно
остальное, что требует шаг выпуска (docs/RELEASING.md: pyflakes по awgbot,
tests, tools). Здесь он идёт с каждым прогоном, а не раз в выпуск, когда
висячая ссылка уже неделями лежит в ветке.

Исключения — только списком ALLOWED, у каждого причина; ненужное исключение —
тоже красное.
"""
from __future__ import annotations

from pathlib import Path

from pyflakes import api, reporter

ROOT = Path(__file__).resolve().parents[2]
DIRS = ("awgbot", "tools", "tests")

# (путь от корня, кусок текста предупреждения) — осознанные исключения
ALLOWED: set[tuple[str, str]] = set()    # исключений нет: модули видов списков грузятся через importlib


class _Collect(reporter.Reporter):
    """Сообщения pyflakes списком «файл:строка: текст» вместо печати."""

    def __init__(self):
        super().__init__(None, None)
        self.found: list[tuple[str, int, str]] = []

    def _rel(self, filename) -> str:
        path = Path(filename).resolve()
        return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)

    def flake(self, message):
        self.found.append((self._rel(message.filename), message.lineno, message.message % message.message_args))

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.found.append((self._rel(filename), lineno or 0, f"синтаксическая ошибка: {msg}"))

    def unexpectedError(self, filename, msg):
        self.found.append((self._rel(filename), 0, f"не прочитан: {msg}"))


def flakes(paths) -> tuple[list[str], set[tuple[str, str]]]:
    """(нарушения «файл:строка: текст» без исключений ALLOWED, сработавшие исключения)."""
    rep = _Collect()
    api.checkRecursive([str(p) for p in paths], rep)
    bad, used = [], set()
    for path, line, text in sorted(rep.found):
        hit = next((a for a in ALLOWED if a[0] == path and a[1] in text), None)
        if hit is not None:
            used.add(hit)
        else:
            bad.append(f"{path}:{line}: {text}")
    return bad, used


def test_pyflakes_finds_nothing_in_the_code_tools_and_tests():
    bad, used = flakes(ROOT / d for d in DIRS)
    assert not bad, "pyflakes:\n" + "\n".join(bad)
    spare = ALLOWED - used
    assert not spare, f"исключение больше не нужно — убери из ALLOWED: {sorted(spare)}"


def test_the_guard_sees_a_dangling_constant(tmp_path):
    """Сторож сторожа: воспроизведение случая _RULE — ветка ссылается на
    константу, которой в модуле нет, — обязано попасть в список; иначе
    зелёный прогон ничего не значит."""
    mod = tmp_path / "routing.py"
    mod.write_text("def kill_switch():\n    return _RULE\n", encoding="utf-8")
    bad, _ = flakes([tmp_path])
    assert bad == [f"{mod.resolve()}:2: undefined name '_RULE'"], bad
