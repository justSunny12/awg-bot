"""zone.py — часть пакета timeutil (см. __init__): метка пояса показа у времени."""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Optional

from awgbot.util import timeutil as pkg          # подменяемое тестами — через пакет


def tz_label(at: Optional[datetime] = None, *, tz: Optional[tzinfo] = None) -> str:
    """«UTC+3», «UTC+5:30», «UTC-3:30», «UTC» — смещение пояса показа (или tz)
    в момент at (None — сейчас): в поясе с летним временем у двух моментов
    одного экрана метки бывают разными, и это верно. Часы без ведущего нуля,
    минуты — только не ноль, минус — ASCII (копируется и ищется), нулевое
    смещение — «UTC» без цифры; секунды смещения округляются до минуты."""
    dt = (at if at is not None else pkg.now()).astimezone(tz or pkg.TZ)
    off = dt.utcoffset()
    minutes = round(off.total_seconds() / 60) if off is not None else 0
    if not minutes:
        return "UTC"
    sign = "-" if minutes < 0 else "+"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def tz_tail(at: Optional[datetime] = None) -> str:
    """« (UTC+3)» — хвост сразу после времени (через пробел, в скобках)."""
    return f" ({tz_label(at)})"
