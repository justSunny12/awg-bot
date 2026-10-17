"""
zone.py — часовой пояс бота: из ключа timezone в app.yaml или пояс хоста.

Пояс показа — пояс хоста, на котором работает роль (то же, что `timedatectl`);
ключ `timezone` — явное переопределение. Читается один раз, на старте
(config), смена пояса хоста вступает после перезапуска бота. Пояс хранения
от этого не зависит — он постоянный, UTC+3 (util/timeutil).

Порядок определения пояса хоста: переменная окружения TZ (без ведущего «:»;
юнит её не ставит — её ставят тесты) → ссылка /etc/localtime (имя — хвост
пути после «zoneinfo/») → /etc/timezone (Debian) → /etc/localtime обычным
файлом (правила из файла, имени нет) → UTC.

Нераспознанный ключ — не отказ старта: бот — единственный канал управления,
лежащий бот хуже сдвинутых меток. Берётся пояс хоста, источник — conf_bad,
значение ключа — в bad_value (предупреждение в журнале и в «🔧 Сервис»).

Модуль без зависимостей от util: его зовёт config на импорте.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timezone, tzinfo
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
        name = _name_from_path(os.path.realpath(localtime)) or _name_from_path(os.readlink(localtime))
        tz = load(name)
        if tz is not None:
            return tz, name
    try:
        with open(timezone_file, encoding="utf-8") as f:
            name = f.read().strip()
    except OSError:
        name = ""
    tz = load(name)
    if tz is not None:
        return tz, name
    try:
        with open(localtime, "rb") as f:
            return ZoneInfo.from_file(f), ""
    except (OSError, ValueError):
        return timezone.utc, "UTC"


def resolve(conf_value: str, **host_kw) -> Zone:
    """Пояс бота: ключ timezone (пустая строка — ключа нет), иначе пояс хоста."""
    host_tz, host_name = host_zone(**host_kw)
    conf_value = (conf_value or "").strip()
    if not conf_value:
        return Zone(host_tz, host_name, "host", host_name, "")
    tz = load(conf_value)
    if tz is None:
        return Zone(host_tz, host_name, "conf_bad", host_name, conf_value)
    return Zone(tz, conf_value, "conf", host_name, "")
