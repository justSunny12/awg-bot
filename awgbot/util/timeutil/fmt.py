"""fmt.py — часть пакета timeutil (см. __init__): форматтеры экранов — даты и
время в поясе показа, возраст, остаток срока, склонения."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from awgbot.util import timeutil as pkg          # подменяемое тестами — через пакет


# ── даты и время ─────────────────────────────────────────────────────────────

def fmt_dt_ui(dt: datetime, ref: Optional[datetime] = None, *, seconds: bool = False,
              label: bool = True) -> str:
    """Дата и время для экранов: «12.10 18:00 (UTC+5)». Год — двумя цифрами и
    только когда он не текущий: «12.10.27 18:00 (UTC+5)». Секунды — только по
    просьбе (правка периода админом) и только когда не :00. Метка пояса — этого
    момента (label=False — без неё)."""
    dt = dt.astimezone(pkg.TZ)
    date = fmt_date_ui(dt, ref)
    clock = dt.strftime("%H:%M") + (dt.strftime(":%S") if seconds and dt.second else "")
    return f"{date} {clock}" + (pkg.tz_tail(dt) if label else "")


def fmt_clock_ui(dt: datetime, *, seconds: bool = False, label: bool = True) -> str:
    """Время без даты: «08:08 (UTC+5)», с секундами — всегда «08:08:14 (UTC+5)»
    (строка «обновлено …», окно ожидания). Метка — этого момента."""
    dt = dt.astimezone(pkg.TZ)
    return dt.strftime("%H:%M:%S" if seconds else "%H:%M") + (pkg.tz_tail(dt) if label else "")


def fmt_hour_ui(hour: int, *, label: bool = True) -> str:
    """Повторяющийся час (тихие часы, автобэкап): «20:00 (UTC+5)»; смещение —
    текущее, кнопки зовут с label=False — «20:00»."""
    return f"{int(hour):02d}:00" + (pkg.tz_tail() if label else "")


def fmt_refreshed(at: datetime, ref: Optional[datetime] = None) -> tuple[str, str]:
    """Строка «обновлено 15.10 08:08:14 (UTC+5) 🔄» по частям: (подпись
    «обновлено 15.10», время «08:08:14 (UTC+5)»). Дата — всегда: на следующий
    день строка без неё врала бы; год — только не текущий."""
    at = at.astimezone(pkg.TZ)
    return f"обновлено {fmt_date_ui(at, ref)}", fmt_clock_ui(at, seconds=True)


def fmt_end_ui(dt: datetime, ref: Optional[datetime] = None) -> str:
    """Дата ОКОНЧАНИЯ срока (подписка, пауза): со временем и меткой пояса только
    в текущем месяце — «12.10 18:00 (UTC+5)»; дальше время не важно — «12.11»,
    «12.10.27» (дата без метки). Исключение — срок завтра: на стыке месяцев час
    отключения терять нельзя."""
    dt = dt.astimezone(pkg.TZ)
    ref = (ref or pkg.now()).astimezone(pkg.TZ)
    if (dt.year, dt.month) == (ref.year, ref.month) or dt.date() <= (ref + timedelta(days=1)).date():
        return fmt_dt_ui(dt, ref)
    return fmt_date_ui(dt, ref)


def fmt_date_ui(dt: datetime, ref: Optional[datetime] = None) -> str:
    """Дата для экранов: «12.10», другой год — «12.10.27»."""
    dt = dt.astimezone(pkg.TZ)
    ref = (ref or pkg.now()).astimezone(pkg.TZ)
    return dt.strftime("%d.%m") + ("" if dt.year == ref.year else dt.strftime(".%y"))


def fmt_period_ui(start: datetime, end: datetime) -> str:
    """«12.09 → 12.10» — начало и конец периода на экранах; границы в разных
    годах — год у обеих: «12.10.25 → 12.10.26»."""
    start, end = start.astimezone(pkg.TZ), end.astimezone(pkg.TZ)
    if start.year != end.year:
        return f"{start.strftime('%d.%m.%y')} → {end.strftime('%d.%m.%y')}"
    return f"{fmt_date_ui(start)} → {fmt_date_ui(end)}"


def first_of_next_month_str() -> str:
    """Дата 1-го числа следующего месяца для экранов: «01.10», в декабре —
    «01.01.27» (год — только не текущий). Используется для «доступ
    приостановлен до …»: месячный сброс наступит именно тогда."""
    n = pkg.now().astimezone(pkg.TZ)
    year, month = (n.year + 1, 1) if n.month == 12 else (n.year, n.month + 1)
    return fmt_date_ui(datetime(year, month, 1, tzinfo=pkg.TZ), n)


def in_quiet_hours(start_hour: int, end_hour: int, moment=None) -> bool:
    """Попадает ли текущий час (пояс показа, стенные часы) в тихое окно. Окно
    может пересекать полночь: если start > end, тихо когда час ≥ start ИЛИ
    час < end (напр. 20→7 = с 20:00 до 07:00). Если start == end — окно пустое
    (False)."""
    h = (moment or pkg.now()).astimezone(pkg.TZ).hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


# ── возраст ──────────────────────────────────────────────────────────────────

def age_short(seconds) -> str:
    """Возраст коротко: «40 с» / «12 мин» / «3 ч» / «12 дн 1 ч». Единицы одни
    на всех: хендшейк линка, снимок, списки, часы шлюза."""
    v = max(0, int(seconds or 0))
    if v < 90:
        return f"{v} с"
    if v < 5400:
        return f"{v // 60} мин"
    if v < 2 * 86400:
        return f"{v // 3600} ч"
    d, rest = divmod(v, 86400)
    h = rest // 3600
    return f"{d} дн {h} ч" if h else f"{d} дн"


def age_ago(seconds) -> str:
    """«40 с назад» / «12 мин назад» / «3 ч назад» / «2 дн назад» — age_short
    с «назад»; меньше секунды — «только что», None — пусто (возраст
    неизвестен)."""
    if seconds is None:
        return ""
    v = int(seconds)
    if v < 1:
        return "только что"
    return age_short(v) + " назад"


def fmt_ago(unix_ts: Optional[int], ref: Optional[datetime] = None) -> str:
    """Давность последнего подключения словами: «только что» (сейчас онлайн),
    «5 мин назад», «3 ч назад», «вчера», дальше — дата «12.09»."""
    if not unix_ts:
        return "никогда"
    if pkg.handshake_is_online(unix_ts, ref):
        return "только что"
    ref = ref or pkg.now()
    age = int(ref.timestamp() - unix_ts)
    if age < 3600:
        return f"{max(1, age // 60)} мин назад"
    if age < 86400:
        return f"{age // 3600} ч назад"
    if age < 2 * 86400:
        return "вчера"
    return fmt_date_ui(datetime.fromtimestamp(unix_ts, tz=timezone.utc), ref)


# ── остаток срока ────────────────────────────────────────────────────────────

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
    secs = pkg.remaining_seconds(end, ref)
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


def remaining_brief(end: datetime, ref: Optional[datetime] = None) -> str:
    """Остаток коротко: «18 дн.», меньше суток — «11 ч», меньше часа — «40
    мин», истекло — «истекло». Дни — вверх: 2 дня 3 часа → «3 дн.»."""
    secs = pkg.remaining_seconds(end, ref)
    if secs <= 0:
        return "истекло"
    if secs >= 86400:
        return f"{pkg.ceil_days(secs)} дн."
    if secs >= 3600:
        return f"{secs // 3600} ч"
    return f"{max(1, secs // 60)} мин"


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


def brief_units(text: str) -> str:
    """«5 дней 3 часа 10 минут» → «5 дн 3 ч 10 мин» — для шапки панели;
    единицы те же, что у age_short."""
    text = re.sub(r"\b(дней|дня|день)\b", "дн", text)
    text = re.sub(r"\b(часов|часа|час)\b", "ч", text)
    return re.sub(r"\b(минут|минуты|минута)\b", "мин", text)
