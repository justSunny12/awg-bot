"""Идентификатор загрузки ядра: меняется ровно один раз — при перезагрузке хоста.

Аптайм для той же цели подводит: бот рестартует несколько раз в первые минуты
после загрузки, а «момент загрузки = сейчас − аптайм» дрожит на секунды.
"""
from __future__ import annotations

import pathlib

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def read_boot_id(path: str = BOOT_ID_PATH) -> str:
    """Пусто — прочитать не удалось (не Linux, нет /proc)."""
    try:
        return pathlib.Path(path).read_text(encoding="ascii").strip()
    except OSError:
        return ""
