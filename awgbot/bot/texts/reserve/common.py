"""common.py — общие помощники текстов резерва: имя сервера «…», время и дата
по поясу показа, причина из состояния с обрезкой (_why). Отдельный лист пакета без встречных импортов — его берут
section (все части), guest, pult, c9wait, peeradmin, pairing и notices; card
из section тянет pult и c9wait, поэтому помощники не живут в section.

Уведомления домена — свой набор в domain/notices/reserve_fmt (там иное
экранирование: кавычки не трогаются)."""
from __future__ import annotations

from awgbot.util import timeutil
from awgbot.util.textfmt.atoms import clip

from ..fmt import _e


WHY_MAX = 100                        # причина или текст ошибки из состояния в строке экрана — не длиннее, с «…»


def _why(text) -> str:
    """Причина или текст ошибки из состояния (исключение, ответ второй стороны —
    до 200 знаков) в строке экрана: обрезана до WHY_MAX с «…» и экранирована. Без
    обрезки карточка «🛟 Резерв» с несколькими причинами разом выходит за 4096."""
    return _e(clip(text, WHY_MAX))


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    """Время «18:00 (UTC+5)»; пусто или не разобрать — пусто."""
    return timeutil.iso_clock(iso)


def _date(iso: str) -> str:
    """Дата и время «12.10 18:00 (UTC+5)»; пусто или не разобрать — пусто."""
    return timeutil.iso_dt(iso)
