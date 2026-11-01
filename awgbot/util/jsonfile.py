"""jsonfile.py — файл состояния JSON-объектом: чтение словаря и атомарная запись.

Читатели состояния (зона резерва, пул Telegram, пара по РФ-трафику, хранилище
резерва у шлюза, сопряжение и смена ключей линка, метки обновления, фиды
транзита) держали по копии «прочитать, на мусоре — пусто, не объект — пусто», а
писатели — по копии записи через общий `<файл>.tmp`. Запись — util/fsatomic.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from awgbot.util import fsatomic


def load(path) -> Any:
    """Разобранный JSON файла как есть; OSError — нет файла или не читается,
    ValueError — не JSON (и не UTF-8). Пустой файл — пустой объект."""
    return json.loads(Path(path).read_text(encoding="utf-8") or "{}")


def read_dict(path) -> dict:
    """Словарь из файла; нет файла, не читается, не JSON или не объект — пусто."""
    try:
        data = load(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write(path, obj, mode: Optional[int] = 0o600, *, fsync: bool = True, sort_keys: bool = False) -> None:
    """obj в path атомарно (util/fsatomic), без экранирования не-ASCII. mode —
    права (по умолчанию 0600 — в состоянии бывают ключи и коды; None — права
    прежнего файла)."""
    fsatomic.write_atomic(path, json.dumps(obj, ensure_ascii=False, sort_keys=sort_keys), mode, fsync)


__all__ = ["load", "read_dict", "write"]
