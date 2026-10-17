"""
zone.py — часовой пояс бота: из ключа timezone в app.yaml или пояс хоста.

Пояс показа — пояс хоста, на котором работает роль (то же, что `timedatectl`);
ключ `timezone` — явное переопределение. Читается один раз, на старте
(config), смена пояса хоста вступает после перезапуска бота. Пояс хранения
от этого не зависит — он постоянный, UTC+3 (util/timeutil).

Порядок определения пояса хоста: переменная окружения TZ (без ведущего «:»;
юнит её не ставит — её ставят тесты) → ссылка /etc/localtime (имя — хвост
цели ссылки после «zoneinfo/», как у timedatectl; полный путь — запасом) →
/etc/localtime обычным файлом (правила из файла; имя — из /etc/timezone, если
правила те же, иначе без имени) → /etc/timezone (нет /etc/localtime) → UTC
с признаком «пояс хоста не определён» (host_known=False).

Нераспознанный ключ — не отказ старта: бот — единственный канал управления,
лежащий бот хуже сдвинутых меток. Берётся пояс хоста, источник — conf_bad,
значение ключа — в bad_value (предупреждение в журнале и в «🔧 Сервис»).

Модуль без зависимостей от util: его зовёт config на импорте.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOCALTIME = "/etc/localtime"
TIMEZONE_FILE = "/etc/timezone"


@dataclass(frozen=True)
class Zone:
    tz: tzinfo          # пояс показа
    name: str           # «Asia/Yekaterinburg»; "" — пояс хоста без имени
    source: str         # "conf" — из ключа, "host" — пояс хоста, "conf_bad" — ключ не распознан
    host_name: str      # имя пояса хоста ("" — без имени)
    bad_value: str      # нераспознанное значение ключа ("" — ключ распознан или его нет)
    host_known: bool = True   # False — пояс хоста не определился, взят UTC


def load(name: str) -> Optional[tzinfo]:
    """Пояс по имени базы поясов или None: имени нет в базе, оно пустое или
    это путь вне базы (ZoneInfo бросает ValueError)."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _name_from_path(path: str) -> str:
    """«/usr/share/zoneinfo/Asia/Yekaterinburg» → «Asia/Yekaterinburg»;
    каталоги posix/ и right/ базы — те же поясы."""
    _, sep, tail = path.partition("zoneinfo/")
    if not sep:
        return ""
    for pre in ("posix/", "right/"):
        if tail.startswith(pre):
            tail = tail[len(pre):]
    return tail


def host_zone(env: Optional[Mapping[str, str]] = None, localtime: str = LOCALTIME,
              timezone_file: str = TIMEZONE_FILE) -> tuple[tzinfo, str]:
    """(пояс, имя) хоста; имя пустое, когда /etc/localtime — копия файла
    пояса, а не ссылка. Ничего не нашлось — UTC с именем «UTC». Пути и
    окружение — параметрами: тесты подменяют их, а не ФС хоста."""
    env = os.environ if env is None else env
    name = (env.get("TZ") or "").strip().lstrip(":")
    tz = load(name)
    if tz is not None:
        return tz, name
    if os.path.islink(localtime):
        # сначала цель ссылки как она записана — так имя берёт timedatectl;
        # полный путь — когда ссылка относительная или ведёт через синоним
        for path in (os.readlink(localtime), os.path.realpath(localtime)):
            name = _name_from_path(path)
            tz = load(name)
            if tz is not None:
                return tz, name
    try:
        with open(timezone_file, encoding="utf-8") as f:
            named = f.read().strip()
    except OSError:
        named = ""
    try:
        with open(localtime, "rb") as f:
            rules = ZoneInfo.from_file(f)
    except (OSError, ValueError):
        rules = None
    if rules is not None:
        # правила — из самого /etc/localtime; имя Debian — только если оно о том же
        named_tz = load(named)
        return (named_tz, named) if named_tz is not None and same_rules(named_tz, rules) else (rules, "")
    tz = load(named)
    if tz is not None:
        return tz, named
    return timezone.utc, "UTC"


def same_rules(a: tzinfo, b: tzinfo, at: Optional[datetime] = None) -> bool:
    """Один и тот же пояс по сути: смещения совпадают сейчас и через каждые три
    месяца года вперёд (синонимы имён, копия файла пояса, летнее время)."""
    at = at or datetime.now(timezone.utc)
    moments = [at + timedelta(days=91 * k) for k in range(5)]
    return all(m.astimezone(a).utcoffset() == m.astimezone(b).utcoffset() for m in moments)


def resolve(conf_value: str, **host_kw) -> Zone:
    """Пояс бота: ключ timezone (пустая строка — ключа нет), иначе пояс хоста."""
    host_tz, host_name = host_zone(**host_kw)
    known = host_tz is not timezone.utc            # запасной UTC — единственный timezone.utc
    conf_value = (conf_value or "").strip()
    if not conf_value:
        return Zone(host_tz, host_name, "host", host_name, "", known)
    tz = load(conf_value)
    if tz is None:
        return Zone(host_tz, host_name, "conf_bad", host_name, conf_value, known)
    return Zone(tz, conf_value, "conf", host_name, "", known)
