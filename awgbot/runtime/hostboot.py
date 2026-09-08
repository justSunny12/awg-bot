"""
hostboot.py — «хост перезагружался?» между двумя стартами бота.

По boot_id ядра, а не по аптайму: аптайм подводит, когда бот рестартует
несколько раз в первые минуты после загрузки. boot_id меняется ровно один раз
за загрузку, и его сравнение с сохранённым даёт точный ответ.
"""
from __future__ import annotations

import pathlib

_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_STATE_KEY = "host_boot_id"


def read_boot_id(path: str = _BOOT_ID_PATH) -> str:
    try:
        return pathlib.Path(path).read_text(encoding="ascii").strip()
    except OSError:
        return ""


def reboot_detected(db, boot_id: str | None = None) -> bool:
    """True, если с прошлого старта бота хост перезагружался. Первый запуск
    (нечего сравнивать) — False; boot_id запоминается в любом случае."""
    cur = boot_id if boot_id is not None else read_boot_id()
    if not cur:
        return False
    prev = db.get_state(_STATE_KEY) or ""
    if prev != cur:
        db.set_state(_STATE_KEY, cur)
    return bool(prev) and prev != cur
