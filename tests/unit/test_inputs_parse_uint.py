"""Unit: разбор числового ввода (bot/inputs.parse_uint) и сторож — обработчики
бота не зовут str.isdigit().

Цена ошибки: isdigit() пропускает «²», «⑤» и арабско-индийские цифры, а int()
на них бросает ValueError — обработчик падает, диалог FSM висит в состоянии
ввода, и человек не получает ни переспроса, ни меню; очень длинное число
проходило до базы и падало OverflowError уже после state.clear().
"""
from __future__ import annotations

import pathlib
import re

import pytest

from awgbot.bot.inputs import DAYS_MAX, PORT_MAX, TRAFFIC_GB_MAX, parse_uint

pytestmark = pytest.mark.unit

BOT = pathlib.Path(__file__).resolve().parents[2] / "awgbot" / "bot"


@pytest.mark.parametrize("raw, expected", [
    ("7", 7), (" 42 ", 42), ("0", 0), ("000", 0), ("999999999", 999_999_999),
    ("²", None), ("⑤", None), ("٣", None), ("１２", None),       # не ASCII-цифры: int() бы их принял или упал
    ("1234567890", None), ("12345678901", None),              # длиннее девяти знаков
    ("", None), (None, None), ("abc", None), ("-1", None), ("+1", None), ("1.5", None), ("1 2", None),
])
def test_parse_uint_accepts_only_short_ascii_digit_strings(raw, expected):
    assert parse_uint(raw) == expected


def test_parse_uint_keeps_the_bounds():
    assert parse_uint("1", 1, 365) == 1 and parse_uint("365", 1, 365) == 365
    assert parse_uint("0", 1, 365) is None and parse_uint("366", 1, 365) is None
    assert parse_uint("5", 10) is None
    assert parse_uint(5) == 5                                 # число из колбэка — тоже годится


def test_the_ceilings_fit_sqlite_integers():
    """Потолки: лимит трафика в байтах и порт — в int SQLite с запасом; дни — десять лет."""
    assert TRAFFIC_GB_MAX * 1024 ** 3 < 2 ** 63 and TRAFFIC_GB_MAX == 1_000_000
    assert PORT_MAX == 65535 and DAYS_MAX == 3650
    assert parse_uint("1000001", 0, TRAFFIC_GB_MAX) is None


def test_bot_handlers_do_not_call_isdigit():
    """Сторож: в awgbot/bot «.isdigit()» живёт только в докстринге inputs.py —
    любое новое место ввода идёт через parse_uint."""
    rx = re.compile(r"\.isdigit\(\)")
    hits = [f"{p.relative_to(BOT.parent.parent)}:{i}"
            for p in sorted(BOT.rglob("*.py")) if p.name != "inputs.py" or p.parent != BOT
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if rx.search(line)]
    assert hits == [], "str.isdigit() в боте — замени на awgbot.bot.inputs.parse_uint:\n" + "\n".join(hits)
