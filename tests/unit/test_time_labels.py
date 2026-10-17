"""Статический сторож: время для человека форматируется только через
util/timeutil — там метка пояса «(UTC+5)». Новое место мимо timeutil вывело бы
время без метки. Ловятся: `strftime` (методом и `datetime.strftime(dt, …)`,
форматом-литералом или константой модуля) с %H/%I/%R/%T/%X/%c; f-строки
«{h:02d}:00», «{h:02d}:{m:02d}», «{dt:%H…}»; %-форматирование «%02d:%02d» и
«%H»; в модулях текстов — `isoformat()` / `now_iso()` / `to_iso()` (это пояс
хранения, а не показа). Область — awgbot/ и tools/. Не ловится `str(dt)` —
типов статически не видно.

Плюс самопроверка сторожа меток в эталонах (tests/screens/timelabels)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
ROOTS = (REPO / "awgbot", REPO / "tools")
SKIP_DIRS = ("awgbot/util/timeutil/",)
# не время для человека: имена файлов, машинные поля, журнал
WHITELIST = {
    "awgbot/domain/backup/archive.py",        # имя файла копии — идентификатор
    "awgbot/infra/awg/server.py",             # ISO UTC для машинного поля
}
TEXT_DIRS = ("awgbot/bot/texts/", "awgbot/domain/notices/", "awgbot/util/textfmt/")
_DIRECTIVE = re.compile(r"%[-_0^#]?[HIRTXc]")
_PCT_CLOCK = re.compile(r"%0?2d:(?:%0?2d|00)")


def _str_consts(tree) -> dict[str, str]:
    """Строковые константы модуля: NAME = "…"."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _value(node, consts) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id, "")
    return ""


def _offences(path: Path, text_module: bool = False) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    consts = _str_consts(tree)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr == "strftime" and node.args:
                fmts = [_value(a, consts) for a in node.args[:2]]
                if any(_DIRECTIVE.search(f) for f in fmts):
                    out.append(f"{node.lineno}: strftime({next(f for f in fmts if _DIRECTIVE.search(f))!r})")
            if text_module and attr in ("isoformat", "now_iso", "to_iso"):
                out.append(f"{node.lineno}: {attr}() в тексте — пояс хранения, не показа")
        if text_module and isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("now_iso", "to_iso"):
            out.append(f"{node.lineno}: {node.func.id}() в тексте — пояс хранения, не показа")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            left = _value(node.left, consts)
            if _PCT_CLOCK.search(left) or _DIRECTIVE.search(left):
                out.append(f"{node.lineno}: %-форматирование времени {left!r}")
        if isinstance(node, ast.JoinedStr):
            vals = node.values
            for a in vals:
                spec = getattr(a, "format_spec", None)
                if isinstance(a, ast.FormattedValue) and isinstance(spec, ast.JoinedStr) \
                        and any(_DIRECTIVE.search(str(getattr(v, "value", ""))) for v in spec.values):
                    out.append(f"{node.lineno}: f-строка «{{…:%H…}}»")

            def _is02(v):
                sp = getattr(v, "format_spec", None)
                return isinstance(v, ast.FormattedValue) and isinstance(sp, ast.JoinedStr) \
                    and [getattr(x, "value", None) for x in sp.values] == ["02d"]
            for i, a in enumerate(vals[:-1]):
                b = vals[i + 1]
                if not (_is02(a) and isinstance(b, ast.Constant)):
                    continue
                if str(b.value).startswith(":00"):
                    out.append(f"{node.lineno}: f-строка «{{…:02d}}:00»")
                elif str(b.value) == ":" and i + 2 < len(vals) and _is02(vals[i + 2]):
                    out.append(f"{node.lineno}: f-строка «{{…:02d}}:{{…:02d}}»")
    return out


def _all():
    for root in ROOTS:
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            yield rel, path


def test_human_time_goes_through_timeutil():
    bad = []
    for rel, path in _all():
        if rel.startswith(SKIP_DIRS) or rel in WHITELIST:
            continue
        bad += [f"{rel}:{o}" for o in _offences(path, rel.startswith(TEXT_DIRS))]
    assert not bad, ("время для человека — через timeutil (fmt_dt_ui, fmt_clock_ui, fmt_hour_ui), "
                     "там метка пояса:\n" + "\n".join(bad))


def test_whitelist_is_still_needed():
    for rel in WHITELIST:
        assert _offences(REPO / rel), f"{rel}: исключение больше не нужно — убери из WHITELIST"


def test_the_guard_catches_every_form(tmp_path):
    p = tmp_path / "x.py"
    p.write_text('FMT = "%d.%m %H:%M"\n'
                 'a = now.strftime("%H:%M")\n'
                 'b = f"С {h:02d}:00"\n'
                 'c = f"{h:02d} ч"\n'
                 'd = now.strftime("%Y-%m")\n'
                 'e = f"{at:%H:%M}"\n'
                 'f = datetime.strftime(now, "%R")\n'
                 'g = now.strftime(FMT)\n'
                 'h = f"{h:02d}:{m:02d}"\n'
                 'i = "%02d:%02d" % (h, m)\n'
                 'j = now.isoformat()\n', encoding="utf-8")
    assert [o.split(":")[0] for o in _offences(p)] == ["2", "3", "6", "7", "8", "9", "10"]
    assert [o.split(":")[0] for o in _offences(p, text_module=True)][-1] == "11"


@pytest.mark.parametrize("line,ok", [
    ("обновлено 15.09 12:00:00 (UTC+5) 🔄", True),
    ('обновлено <a href="x">15.09 12:00:00 (UTC+5) 🔄</a>', True),
    ("Тихие часы 20:00–07:00 (UTC+5) — без звука", True),
    ("✅ Автобэкап: 1-е, 12:00 → 5-е, 03:00 (UTC+5)", True),
    ("с 15.09 12:00 (UTC+5) до 15.10 18:00 (UTC+5)", True),
    ("Часовой пояс — Asia/Yekaterinburg (UTC+5), пояс сервера", True),
    ("адрес 10.8.1.5:51820 и <code>12:00</code>", True),
    ("<blockquote>выпуск: в 12:00</blockquote>", True),
    ("ост. 3 дн. · 5 мин назад", True),
    ("Тихие часы 20:00–07:00 МСК", False),
    ("Продлено до 17.09 12:00", False),
    ("с 15.09 12:00 (UTC+5) до 15.10 18:00", False),
    ("с 15.09 12:00 до 15.10 18:00 (UTC+5)", False),                   # у каждого момента — своя
    ('обновлено <a href="x">12:00 🔄</a> (UTC+5)', False),              # метка снаружи ссылки
    ("до 12:00 (UTC+3)", False),                                        # не тот пояс
    ("до 12.10 (UTC+5)", False),                                        # метка у даты
    ("<blockquote expandable>применена 15.09 12:00</blockquote>", False),
])
def test_screen_guard(line, ok):
    from tests.screens.timelabels import problems_in
    assert (problems_in(line, {"UTC+5"}) == []) is ok, line
