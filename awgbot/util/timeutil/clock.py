"""clock.py — часть пакета timeutil (см. __init__): «сейчас», ISO-сериализация
(пояс хранения — UTC+3), ввод даты человеком, периоды и арифметика моментов
через абсолютное время."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from dateutil.relativedelta import relativedelta

from awgbot.util import timeutil as pkg          # подменяемое тестами — через пакет


# ── «сейчас», ISO ────────────────────────────────────────────────────────────

def now() -> datetime:
    """Текущий момент как aware-datetime в поясе показа."""
    return datetime.now(pkg.TZ)


def now_iso() -> str:
    """Текущий момент ISO-строкой в поясе хранения (UTC+3, «+03:00»). Пишется в БД."""
    return to_iso(pkg.now())


def to_iso(dt: datetime) -> str:
    """Aware-datetime → ISO-строка в поясе хранения (UTC+3, «+03:00»), в каком
    бы поясе бот ни показывал время. Наивное — как время пояса показа."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pkg.TZ)
    return dt.astimezone(pkg.STORE_TZ).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    """ISO-строка из БД или линка (любое смещение) → aware-datetime в поясе
    показа. Наивные строки (без смещения, наследие) — как UTC+3: так они и
    писались."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pkg.STORE_TZ)
    return dt.astimezone(pkg.TZ)


def parse_dt_sec(s: str) -> datetime:
    """«DD.MM.YYYY HH:MM:SS», «DD.MM.YYYY HH:MM» или «DD.MM.YYYY» (недостающее
    время — нули) — стенные часы пояса показа → aware-datetime. Время, которого
    в этот день нет (весенний переход), сдвигается вперёд на величину перехода;
    повторное (осенний) — первое из двух. Бросает ValueError при неверном
    формате (ловит вызывающий, просит повторить ввод)."""
    raw = " ".join(s.split())
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            wall = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        # fold=0: повторный час — первое из двух, пропавший — по смещению до
        # перехода; круг через UTC даёт настоящие стенные часы этого момента
        return wall.replace(tzinfo=pkg.TZ).astimezone(timezone.utc).astimezone(pkg.TZ)
    raise ValueError(f"не дата: {s!r}")


# ── арифметика моментов ──────────────────────────────────────────────────────

def elapsed(since: datetime, until: Optional[datetime] = None) -> float:
    """Секунды от since до until (None — сейчас) по абсолютному времени:
    отрицательное — until раньше since. Вычитание дат одного пояса считает по
    стенным часам и врёт на час через переход на летнее время."""
    return (until if until is not None else pkg.now()).timestamp() - since.timestamp()


def shift(dt: datetime, seconds: float) -> datetime:
    """Момент dt, сдвинутый на seconds настоящих секунд, в поясе показа
    («сейчас минус возраст снимка»)."""
    return datetime.fromtimestamp(dt.timestamp() + seconds, pkg.TZ)


# ── периоды (календарные месяц/год через relativedelta) ──────────────────────

_PERIOD_DELTAS = {
    "day": relativedelta(days=1),
    "week": relativedelta(weeks=1),
    "month": relativedelta(months=1),
    "year": relativedelta(years=1),
}


def add_period(start: datetime, kind: str, extra_seconds: int = 0) -> datetime:
    """start + длительность(kind) [+ сохранённый остаток в секундах].

    Месяц и год — календарные (relativedelta), день и неделя — по стенным
    часам: подписка кончается в тот же час на циферблате и через переход на
    летнее время. extra_seconds — неистраченный остаток при продлении с
    сохранением.
    """
    if kind not in _PERIOD_DELTAS:
        raise ValueError(f"Неизвестный период: {kind}")
    end = start + _PERIOD_DELTAS[kind]
    if extra_seconds:
        end = end + relativedelta(seconds=extra_seconds)
    return end


def period_minutes(start: datetime, end: datetime) -> int:
    """Длительность периода в минутах (для фильтрации порогов уведомлений)."""
    return int(elapsed(start, end) // 60)


def remaining_seconds(end: datetime, ref: Optional[datetime] = None) -> int:
    """Сколько секунд до end от ref (или сейчас). Отрицательное = уже истекло."""
    return int(elapsed(ref if ref is not None else pkg.now(), end))


def ceil_days(delta_seconds: float) -> int:
    """Секунды → целые дни, округление ВВЕРХ (ceil). 0 сек → 0 дней, 1 сек → 1."""
    return int(math.ceil(delta_seconds / 86400.0))
