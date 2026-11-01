"""Момент из ISO-строки в текстах — timeutil.iso_clock / iso_dt / iso_date_yy и
один набор помощников текстов резерва.

Что стережём: строка из БД, состояния или линка показывается в поясе показа
(пояс бота, в тестах — UTC+5), какое бы смещение в ней ни стояло; пустая или
битая строка даёт заданную заглушку, а не трассировку посреди уведомления.
Копии этих помощников по модулям (свои _when/_date/_n в каждом тексте резерва,
в бэкапе, в вопросах шлюза) разъезжались: где-то ловили только ValueError, где-то
дата шла без пояса. Цена ошибки — время «с …» в чужом поясе (час-другой мимо,
у полуночи — другая дата) или упавшее уведомление вместо строки без момента.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from awgbot.util import timeutil as t

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2] / "awgbot"
LATE = "2026-11-15T22:30:00+03:00"          # в поясе показа (UTC+5) — уже 16.11, 00:30


def test_the_moment_is_shown_in_the_display_zone_whatever_offset_it_was_written_with():
    assert t.iso_clock(LATE) == "00:30 (UTC+5)"
    assert t.iso_clock("2026-11-15T19:30:00+00:00") == "00:30 (UTC+5)"
    assert t.iso_clock(LATE, seconds=True) == "00:30:00 (UTC+5)"
    assert t.iso_dt(LATE, year=True) == "16.11.26 00:30 (UTC+5)"
    assert t.iso_date_yy(LATE) == "16.11.26", "дата у полуночи — по поясу показа, а не по смещению строки"


@pytest.mark.parametrize("bad", ["", None, "вчера", "2026-13-01T00:00:00+03:00", 0])
def test_an_empty_or_broken_moment_gives_the_fallback(bad):
    assert t.iso_clock(bad) == "" and t.iso_dt(bad) == "" and t.iso_date_yy(bad) == ""
    assert t.iso_clock(bad, "?") == "?" and t.iso_dt(bad, "?", year=True) == "?" and t.iso_date_yy(bad, "?") == "?"


def test_texts_take_the_moment_through_the_shared_helpers():
    """Тексты резерва, бэкапа, вопросов шлюза и строки главной — одним путём: те же
    строки, тот же пояс, та же заглушка."""
    from awgbot.bot.texts.gateway import ask
    from awgbot.bot.texts.reserve import c9wait, common, peeradmin, pult
    from awgbot.domain.notices import backup, reserve_fmt, reserve_tail, reserve_tg
    from awgbot.domain.services.reserve import away
    assert common._when(LATE) == reserve_fmt._when(LATE) == away._when(LATE) == "00:30 (UTC+5)"
    assert common._date(LATE) == away._date(LATE) == "16.11 00:30 (UTC+5)"
    assert backup._when(LATE) == "16.11.26 00:30 (UTC+5)"
    assert backup._date(LATE) == ask._date(LATE) == reserve_tail._date_yy(LATE) == "16.11.26"
    assert common._when("") == reserve_fmt._when(None) == "" and away._when("") == away._date("x") == "?"
    assert backup._date("") == backup._when("") == ask._date(None) == "?"
    assert reserve_tail._date_yy("2026-11-15") == "15.11.26" and reserve_tail._date_yy("Х") == "Х"
    assert "с 16.11" in reserve_tg.tg_pool_stale(LATE), "дата застоя анонсов — по поясу показа"
    assert pult._when is common._when and peeradmin._when is common._when and c9wait._date is common._date


def _defs(path: pathlib.Path) -> set[str]:
    return {n.name for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef)}


def test_reserve_texts_keep_one_set_of_name_and_time_helpers():
    """Имя «…» и время в текстах резерва — из bot/texts/reserve/common (бот) и
    domain/notices/reserve_fmt (уведомления домена: экранирование без кавычек);
    своих копий у соседей нет, и уведомления берут их не через фасад reserve."""
    texts = ROOT / "bot" / "texts" / "reserve"
    for path in sorted(texts.rglob("*.py")):
        if path.name != "common.py":
            assert not _defs(path) & {"_n", "_when", "_date"}, f"своя копия помощника в {path.relative_to(ROOT)}"
    notices = ROOT / "domain" / "notices"
    for path in sorted(notices.glob("reserve*.py")):
        if path.name != "reserve_fmt.py":
            assert not _defs(path) & {"_e", "_n", "_when"}, f"своя копия помощника в {path.relative_to(ROOT)}"
    for name in ("reserve_keep.py", "reserve_pult.py", "reserve_tail.py"):
        tree = ast.parse((notices / name).read_text())
        assert not [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.level == 1 and n.module == "reserve"], \
            f"{name} берёт помощники через фасад reserve — встречный импорт пакета"


def test_the_return_mode_is_read_in_one_place():
    """Ключ рычага возврата читает только reserve/away.return_mode; outbound.py
    ждёт своей очереди (его делит соседняя ветка) — в списке исключений до тех пор."""
    allowed = {ROOT / "domain" / "services" / "reserve" / "away.py",
               ROOT / "domain" / "services" / "reserve" / "outbound.py"}
    hits = [p for p in ROOT.rglob("*.py") if '"app.reserve.return"' in p.read_text() and p not in allowed]
    assert not hits, f"ключ рычага возврата читают мимо return_mode: {hits}"
