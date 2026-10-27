"""common.py — часть пакета main (см. __init__): журнал, пояс на старте и метка
свежей установки в каталоге данных."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Mapping, Optional

FRESH_INSTALL_MARKER = ".fresh-install"   # кладёт установщик агента (awg-bot.sh), снимает первый запуск

LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"

log = logging.getLogger("awgbot.main")


class ZonedFormatter(logging.Formatter):
    """Время записи — ISO-8601 в поясе бота со смещением:
    «2026-10-17T08:08:14.123+05:00 INFO awgbot.main: …» — тот же пояс, что в
    чате, и смещение написано."""

    def formatTime(self, record, datefmt=None):
        from awgbot.util import timeutil
        return datetime.fromtimestamp(record.created, timeutil.TZ).isoformat(timespec="milliseconds")


def under_journald(env: Optional[Mapping[str, str]] = None, fd: int = 2) -> bool:
    """Поток ошибок процесса идёт в journald: JOURNAL_STREAM («устройство:inode»)
    совпадает с самим потоком — systemd ставит переменную юниту, а дочерний
    процесс с перенаправленным выводом её только наследует."""
    env = os.environ if env is None else env
    raw = env.get("JOURNAL_STREAM") or ""
    dev, _, ino = raw.partition(":")
    if not (dev.isdigit() and ino.isdigit()):
        return False
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == (int(dev), int(ino))


def log_formatter(env: Optional[Mapping[str, str]] = None, fd: int = 2) -> logging.Formatter:
    """Под journald — без времени в строке: его ставит journald
    (`journalctl -o short-iso` — со смещением); вне — ISO со смещением."""
    if under_journald(env, fd):
        return logging.Formatter(LOG_FORMAT)
    return ZonedFormatter("%(asctime)s " + LOG_FORMAT)


def log_zone() -> None:
    """Одна строка о поясе на старте; нераспознанный ключ — предупреждением."""
    from awgbot.core import config
    from awgbot.util import timeutil
    shown = config.TZ_NAME or "без имени"
    if not getattr(config, "TZ_HOST_KNOWN", True):
        log.warning("пояс хоста не определён (нет TZ, /etc/localtime, /etc/timezone) — беру UTC; "
                    "задай пояс: timedatectl set-timezone <пояс>")
    if config.TZ_SOURCE == "conf_bad":
        log.warning("часовой пояс %r из app.yaml не распознан — беру пояс хоста %s (%s)",
                    config.TZ_BAD_VALUE, shown, timeutil.tz_label())
    elif config.TZ_SOURCE == "conf":
        log.info("часовой пояс: %s (%s), ключ timezone в app.yaml; у хоста — %s",
                 shown, timeutil.tz_label(), config.TZ_HOST_NAME or "без имени")
    else:
        log.info("часовой пояс: %s (%s), пояс хоста", shown, timeutil.tz_label())
