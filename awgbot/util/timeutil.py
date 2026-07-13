"""
timeutil.py — единый источник времени (UTC+3) и все форматтеры.

Правило проекта: любое «сейчас» и любое отображение времени идёт отсюда.
Внутри контейнера время в UTC (mtime файлов и т.п.) — но awg show отдаёт
handshake в unix-времени (TZ-независимо), а периоды/сроки/бэкапы мы считаем
и показываем в UTC+3. Единая точка исключает сдвиги на 3 часа.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from dateutil.relativedelta import relativedelta

from awgbot.core import config

TZ = config.TZ

# Английские сокращения для формата даты Amnezia (Qt Qt::TextDate, C-locale),
# хардкодим, чтобы не зависеть от системной локали.
_WDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]      # weekday(): Mon=0
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ─────────────────────────────────────────────────────────────────────────────
# «Сейчас», ISO-сериализация
# ─────────────────────────────────────────────────────────────────────────────

def now() -> datetime:
    """Текущий момент как aware-datetime в UTC+3."""
    return datetime.now(TZ)


def now_iso() -> str:
    """Текущий момент как ISO-строка (UTC+3, с оффсетом). Пишется в БД."""
    return now().isoformat(timespec="seconds")


def to_iso(dt: datetime) -> str:
    """Aware-datetime → ISO-строка в UTC+3."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    """ISO-строка из БД → aware-datetime в UTC+3.
    Наивные строки (без оффсета, legacy) трактуем как UTC+3."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


# ─────────────────────────────────────────────────────────────────────────────
# Периоды (календарные месяц/год через relativedelta)
# ─────────────────────────────────────────────────────────────────────────────

_PERIOD_DELTAS = {
    "day": relativedelta(days=1),
    "week": relativedelta(weeks=1),
    "month": relativedelta(months=1),
    "year": relativedelta(years=1),
}


def add_period(start: datetime, kind: str, extra_seconds: int = 0) -> datetime:
    """start + длительность(kind) [+ сохранённый остаток в секундах].

    Месяц и год — календарные (relativedelta), день/неделя — фиксированные.
    extra_seconds — неистраченный остаток при продлении с сохранением.
    """
    if kind not in _PERIOD_DELTAS:
        raise ValueError(f"Неизвестный период: {kind}")
    end = start + _PERIOD_DELTAS[kind]
    if extra_seconds:
        end = end + relativedelta(seconds=extra_seconds)
    return end


def period_minutes(start: datetime, end: datetime) -> int:
    """Длительность периода в минутах (для фильтрации порогов уведомлений)."""
    return int((end - start).total_seconds() // 60)


def remaining_seconds(end: datetime, ref: Optional[datetime] = None) -> int:
    """Сколько секунд до end от ref (или сейчас). Отрицательное = уже истекло."""
    ref = ref or now()
    return int((end - ref).total_seconds())


# ─────────────────────────────────────────────────────────────────────────────
# Форматтеры отображения
# ─────────────────────────────────────────────────────────────────────────────

def fmt_dt(dt: datetime) -> str:
    """DD.MM.YYYY HH:MM (в UTC+3)."""
    dt = dt.astimezone(TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def fmt_dt_sec(dt: datetime) -> str:
    """DD.MM.YYYY HH:MM:SS (в UTC+3) — для ручной правки периода админом."""
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M:%S")


def parse_dt_sec(s: str) -> datetime:
    """«DD.MM.YYYY HH:MM:SS» → aware-datetime в UTC+3. Бросает ValueError при
    неверном формате (ловит вызывающий, просит повторить ввод)."""
    dt = datetime.strptime(s.strip(), "%d.%m.%Y %H:%M:%S")
    return dt.replace(tzinfo=TZ)


def first_of_next_month_str() -> str:
    """Дата 1-го числа следующего месяца как «DD.MM.YYYY» (UTC+3). Используется
    для «доступ приостановлен до …»: месячный сброс наступит именно тогда."""
    n = now().astimezone(TZ)
    year, month = (n.year + 1, 1) if n.month == 12 else (n.year, n.month + 1)
    return f"01.{month:02d}.{year}"


def ceil_days(delta_seconds: float) -> int:
    """Секунды → целые дни, округление ВВЕРХ (ceil). 0 сек → 0 дней, 1 сек → 1."""
    import math
    return int(math.ceil(delta_seconds / 86400.0))


def in_quiet_hours(start_hour: int, end_hour: int, moment=None) -> bool:
    """Попадает ли текущий час (UTC+3) в тихое окно. Окно может пересекать
    полночь: если start > end, тихо когда час ≥ start ИЛИ час < end
    (напр. 20→7 = с 20:00 до 07:00). Если start == end — окно пустое (False)."""
    h = (moment or now()).astimezone(TZ).hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


def fmt_period(start: datetime, end: datetime) -> str:
    """DD.MM.YYYY HH:MM → DD.MM.YYYY HH:MM."""
    return f"{fmt_dt(start)} → {fmt_dt(end)}"


def _plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Русские склонения: forms=(1, 2-4, 5+). Напр. ('день','дня','дней')."""
    n_abs = abs(n)
    n100 = n_abs % 100
    n10 = n_abs % 10
    if 10 < n100 < 20:
        return forms[2]
    if n10 == 1:
        return forms[0]
    if 1 < n10 < 5:
        return forms[1]
    return forms[2]


def fmt_remaining(end: datetime, ref: Optional[datetime] = None) -> str:
    """«5 дней 3 часа 10 минут» — нулевые компоненты отбрасываются.

    Истекло → «истекло». Меньше минуты, но не истекло → «меньше минуты».
    """
    secs = remaining_seconds(end, ref)
    if secs <= 0:
        return "истекло"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    parts: list[str] = []
    if days:
        parts.append(f"{days} {_plural_ru(days, ('день', 'дня', 'дней'))}")
    if hours:
        parts.append(f"{hours} {_plural_ru(hours, ('час', 'часа', 'часов'))}")
    if minutes:
        parts.append(f"{minutes} {_plural_ru(minutes, ('минута', 'минуты', 'минут'))}")
    if not parts:
        return "меньше минуты"
    return " ".join(parts)


def fmt_remaining_short(seconds: int) -> str:
    """То же, но из готового числа секунд (для диалога сохранения остатка:
    «Сохранить неистраченный остаток (X дней Y часов)?»). Показывает дни+часы."""
    if seconds <= 0:
        return "0 минут"
    days, rem = divmod(seconds, 86400)
    hours = rem // 3600
    parts: list[str] = []
    if days:
        parts.append(f"{days} {_plural_ru(days, ('день', 'дня', 'дней'))}")
    if hours:
        parts.append(f"{hours} {_plural_ru(hours, ('час', 'часа', 'часов'))}")
    if not parts:
        minutes = max(1, seconds // 60)
        return f"{minutes} {_plural_ru(minutes, ('минута', 'минуты', 'минут'))}"
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Handshake (unix) → онлайн/оффлайн и «последний коннект»
# ─────────────────────────────────────────────────────────────────────────────

def handshake_is_online(unix_ts: Optional[int], ref: Optional[datetime] = None) -> bool:
    """Онлайн, если последний handshake свежее порога ONLINE_HANDSHAKE_SECONDS."""
    if not unix_ts:
        return False
    ref = ref or now()
    age = ref.timestamp() - unix_ts
    return 0 <= age <= config.ONLINE_HANDSHAKE_SECONDS


def fmt_handshake(unix_ts: Optional[int]) -> str:
    """unix → «DD.MM.YYYY HH:MM» (UTC+3). None/0 → «никогда».
    Свежий handshake (устройство сейчас онлайн) → «только что»: точный
    таймстамп для живого подключения — шум, WG обновляет его каждые ~2 мин."""
    if not unix_ts:
        return "никогда"
    if handshake_is_online(unix_ts):
        return "только что"
    dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone(TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


# ─────────────────────────────────────────────────────────────────────────────
# Формат даты Amnezia для clientsTable (Qt::TextDate, напр. «Sun Jul 5 19:08:02 2026»)
# День — БЕЗ паддинга, локальное время UTC+3, английские сокращения.
# ─────────────────────────────────────────────────────────────────────────────

def amnezia_date(dt: Optional[datetime] = None) -> str:
    """Дата в формате, которым приложение Amnezia пишет creationDate.
    Пример: «Sun Jul 5 19:08:02 2026». Нужен для нативной записи в clientsTable."""
    dt = (dt or now()).astimezone(TZ)
    wday = _WDAYS[dt.weekday()]
    month = _MONTHS[dt.month - 1]
    day = str(dt.day)  # без нуля/пробела впереди
    return f"{wday} {month} {day} {dt.strftime('%H:%M:%S')} {dt.year}"


# ─────────────────────────────────────────────────────────────────────────────
# Docker StartedAt (для аптайма) и длительности
# ─────────────────────────────────────────────────────────────────────────────

def parse_docker_time(s: str) -> Optional[datetime]:
    """Docker StartedAt (напр. '2026-07-05T10:39:22.158372221Z') → aware datetime.
    Обрезает наносекунды до микросекунд (fromisoformat не ест 9 знаков)."""
    if not s or s.startswith("0001"):        # 0001-01-01 = «не запускался»
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, frac = s.split(".", 1)
        tzpart = ""
        for sep in ("+", "-"):
            if sep in frac:
                idx = frac.index(sep)
                tzpart = frac[idx:]
                frac = frac[:idx]
                break
        frac = frac[:6]
        s = f"{head}.{frac}{tzpart}"
    try:
        return datetime.fromisoformat(s).astimezone(TZ)
    except ValueError:
        return None


def fmt_uptime(started: Optional[datetime]) -> str:
    """Аптайм от started до сейчас: «5 дней 3 часа» (дни+часы)."""
    if started is None:
        return "?"
    secs = int((now() - started).total_seconds())
    return fmt_remaining_short(secs)


__all__ = [
    "TZ", "now", "now_iso", "to_iso", "parse_iso",
    "add_period", "period_minutes", "remaining_seconds",
    "fmt_dt", "fmt_period", "fmt_remaining", "fmt_remaining_short",
    "handshake_is_online", "fmt_handshake", "amnezia_date",
    "parse_docker_time", "fmt_uptime",
]