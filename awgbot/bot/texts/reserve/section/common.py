"""common.py — часть пакета section (см. __init__): общие помощники строк
раздела и карточки — имя сервера «…», время и дата по поясу показа. Отдельно —
их берут все части пакета и guest (бот основы на резерве)."""
from __future__ import annotations

from awgbot.util import timeutil

from ...fmt import _e


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except ValueError:
        return ""


def _date(iso: str) -> str:
    try:
        return timeutil.fmt_dt_ui(timeutil.parse_iso(iso))
    except ValueError:
        return ""
