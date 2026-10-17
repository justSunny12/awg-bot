"""awg.py — часть пакета timeutil (см. __init__): хендшейк (unix-время) →
онлайн и «последнее подключение», время старта Docker, аптайм."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from awgbot.core import settings
from awgbot.util import timeutil as pkg          # подменяемое тестами — через пакет


def handshake_is_online(unix_ts: Optional[int], ref: Optional[datetime] = None,
                        *, threshold: Optional[int] = None) -> bool:
    """Онлайн, если последний handshake свежее порога ONLINE_HANDSHAKE_SECONDS.

    threshold — порог в секундах, ТОЛЬКО именованным аргументом. Так его
    передаёт код, который считает онлайн для пачки устройств и не хочет читать
    настройку на каждое. Позиционно там оказывался ref, и вместо даты в него
    прилетало число: карточка профиля падала на ровном месте.
    """
    if not unix_ts:
        return False
    ref = ref or pkg.now()
    age = ref.timestamp() - unix_ts
    limit = (threshold if threshold is not None
             else settings.get_int("app.online_handshake_seconds", 300))
    return 0 <= age <= limit


def fmt_handshake(unix_ts: Optional[int]) -> str:
    """unix → «DD.MM.YYYY HH:MM» (пояс показа). None/0 → «никогда».
    Свежий handshake (устройство сейчас онлайн) → «только что»: точный
    таймстамп для живого подключения — шум, WG обновляет его каждые ~2 мин."""
    if not unix_ts:
        return "никогда"
    if pkg.handshake_is_online(unix_ts):
        return "только что"
    dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone(pkg.TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


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
        return datetime.fromisoformat(s).astimezone(pkg.TZ)
    except ValueError:
        return None


def fmt_uptime(started: Optional[datetime]) -> str:
    """Аптайм от started до сейчас: «5 дней 3 часа» (дни+часы)."""
    if started is None:
        return "?"
    return pkg.fmt_remaining_short(int(pkg.elapsed(started)))
