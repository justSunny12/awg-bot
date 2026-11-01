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
    """Время «18:00 (UTC+5)»; пусто или не разобрать — пусто."""
    return timeutil.iso_clock(iso)
