"""
timeutil/ — единый источник времени и все форматтеры.

Правило проекта: любое «сейчас» и любое отображение времени идёт отсюда.

Два пояса:
  • пояс хранения — UTC+3, постоянный (STORE_TZ): всё, что пишется в БД и
    состояние (to_iso, now_iso), — ISO-8601 со смещением +03:00, в каком бы
    поясе бот ни показывал время. Строки одного смещения сравниваются текстом
    (SQL-пороги, сортировки) без ошибки. Читается любое смещение (parse_iso);
    наивные строки без смещения (наследие) — как UTC+3;
  • пояс показа — пояс хоста или ключ timezone в app.yaml (TZ = config.TZ,
    core/zone.py): в нём «сейчас», форматтеры, расписания и тихие часы.
Моменты из линка — ISO со смещением или unix-время; показ пересчитывает их в
пояс показа.

Арифметика — через абсолютное время: Python для двух aware-дат с одним и тем
же tzinfo считает разность и сравнение по стенным часам, и в поясе с летним
временем интервал через переход врёт на час. Разность моментов — elapsed(),
сдвиг момента на секунды — shift().

Части пакета: zone — метка пояса; clock — «сейчас», ISO, ввод, периоды,
арифметика; fmt — форматтеры экранов; awg — хендшейк, Docker, аптайм. Части
зовут через пакет всё, что подменяют тесты (pkg.now(), pkg.TZ): заморозка
часов в эталонах подменяет timeutil.now, и форматтеры соседнего модуля
обязаны её видеть.
"""

from __future__ import annotations

from datetime import timedelta, timezone

from awgbot.core import config

TZ = config.TZ                                   # пояс показа
STORE_TZ = timezone(timedelta(hours=3))          # пояс хранения — UTC+3, постоянный

from .zone import tz_label, tz_tail  # noqa: E402
from .clock import (now, now_iso, to_iso, parse_iso, parse_dt_sec, elapsed, shift,  # noqa: E402
                    add_period, period_minutes, remaining_seconds, ceil_days)
from .fmt import (fmt_dt_ui, fmt_end_ui, fmt_date_ui, fmt_period_ui, age_short, age_ago,  # noqa: E402
                  fmt_ago, first_of_next_month_str, in_quiet_hours, _plural_ru, fmt_remaining,
                  remaining_brief, fmt_remaining_short, brief_units)
from .awg import handshake_is_online, fmt_handshake, parse_docker_time, fmt_uptime  # noqa: E402

__all__ = [
    "TZ", "STORE_TZ", "tz_label", "tz_tail",
    "now", "now_iso", "to_iso", "parse_iso", "parse_dt_sec", "elapsed", "shift",
    "add_period", "period_minutes", "remaining_seconds", "ceil_days",
    "fmt_dt_ui", "fmt_end_ui", "fmt_date_ui", "fmt_period_ui", "age_short", "age_ago",
    "fmt_ago", "first_of_next_month_str", "in_quiet_hours", "_plural_ru", "fmt_remaining",
    "remaining_brief", "fmt_remaining_short", "brief_units",
    "handshake_is_online", "fmt_handshake", "parse_docker_time", "fmt_uptime",
]
