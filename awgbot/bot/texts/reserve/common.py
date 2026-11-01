"""common.py — общие помощники текстов резерва: имя сервера «…», время и дата
по поясу показа. Отдельный лист пакета без встречных импортов — его берут
section (все части), guest, pult, c9wait, peeradmin, pairing и notices; card
из section тянет pult и c9wait, поэтому помощники не живут в section.

Уведомления домена — свой набор в domain/notices/reserve_fmt (там иное
экранирование: кавычки не трогаются)."""
from __future__ import annotations

from awgbot.util import timeutil

from ..fmt import _e


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    """Время «18:00 (UTC+5)»; пусто или не разобрать — пусто."""
    return timeutil.iso_clock(iso)


def _date(iso: str) -> str:
    """Дата и время «12.10 18:00 (UTC+5)»; пусто или не разобрать — пусто."""
    return timeutil.iso_dt(iso)
