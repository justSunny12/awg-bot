"""Статический сторож: время для человека форматируется только через
util/timeutil — там метка пояса «(UTC+5)». Новое место с `strftime("%H…")`
или f-строкой «{h:02d}:00» мимо timeutil вывело бы время без метки.

Плюс самопроверка сторожа меток в эталонах (tests/screens/timelabels)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "awgbot"
SKIP_DIRS = ("util/timeutil/",)
# не время для человека: имена файлов и машинные поля
WHITELIST = {
    "domain/backupcrypto.py",          # имя файла копии — идентификатор
    "infra/awg/server.py",             # ISO UTC для машинного поля
}


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "strftime" and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) \
                and "%H" in node.args[0].value:
            out.append(f"{node.lineno}: strftime({node.args[0].value!r})")
        if isinstance(node, ast.JoinedStr):
            vals = node.values
            for a in vals:
                spec = getattr(a, "format_spec", None)
                if isinstance(a, ast.FormattedValue) and isinstance(spec, ast.JoinedStr) \
                        and any("%H" in str(getattr(v, "value", "")) for v in spec.values):
                    out.append(f"{node.lineno}: f-строка «{{…:%H…}}»")
            for a, b in zip(vals, vals[1:]):
                spec = getattr(a, "format_spec", None)
                if isinstance(a, ast.FormattedValue) and isinstance(spec, ast.JoinedStr) \
                        and [getattr(v, "value", None) for v in spec.values] == ["02d"] \
                        and isinstance(b, ast.Constant) and str(b.value).startswith(":00"):
                    out.append(f"{node.lineno}: f-строка «{{…:02d}}:00»")
    return out


def test_human_time_goes_through_timeutil():
    bad = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(SKIP_DIRS) or rel in WHITELIST:
            continue
        bad += [f"awgbot/{rel}:{o}" for o in _offences(path)]
    assert not bad, ("время для человека — через timeutil (fmt_dt_ui, fmt_clock_ui, fmt_hour_ui), "
                     "там метка пояса:\n" + "\n".join(bad))


def test_whitelist_is_still_needed():
    for rel in WHITELIST:
        assert _offences(ROOT / rel), f"{rel}: исключение больше не нужно — убери из WHITELIST"


def test_the_guard_catches_both_forms(tmp_path):
    p = tmp_path / "x.py"
    p.write_text('a = now.strftime("%H:%M")\nb = f"С {h:02d}:00"\nc = f"{h:02d} ч"\n'
                 'd = now.strftime("%Y-%m")\ne = f"{at:%H:%M}"\n', encoding="utf-8")
    assert [o.split(":")[0] for o in _offences(p)] == ["1", "2", "5"]


@pytest.mark.parametrize("line,ok", [
    ("обновлено 15.09 12:00:00 (UTC+5) 🔄", True),
    ("Тихие часы 20:00–07:00 (UTC+5) — без звука", True),
    ("с 15.09 12:00 (UTC+5:30) до 15.10 18:00 (UTC-3:30)", True),
    ("главная в поясе 07:00:00 (UTC) 🔄", True),
    ("Тихие часы 20:00–07:00 МСК", False),
    ("Продлено до 17.09 12:00", False),
    ("с 15.09 12:00 (UTC+5) до 15.10 18:00", False),
    ("адрес 10.8.1.5:51820 и <code>12:00</code>", True),
    ("<blockquote>выпуск: в 12:00</blockquote>", True),
    ("<blockquote expandable>применена 15.09 12:00</blockquote>", False),
    ("ост. 3 дн. · 5 мин назад", True),
])
def test_screen_guard(line, ok):
    from tests.screens.timelabels import unlabeled
    assert (unlabeled(line) == []) is ok, line
