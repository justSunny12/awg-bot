"""reserve_fmt.py — часть пакета notices (см. __init__): общие помощники
уведомлений пары серверов — экранирование, имя сервера «…», время по поясу
показа. Отдельно — их берут все части reserve_* и сам reserve; общий модуль
без встречного импорта фасада."""
from __future__ import annotations

import html

from awgbot.util import timeutil


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except (ValueError, TypeError):
        return ""
