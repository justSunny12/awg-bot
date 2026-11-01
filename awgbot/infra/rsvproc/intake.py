"""intake.py — часть пакета rsvproc (см. __init__): вход процесса открытия из
stdin — первая строка JSON (или весь stdin при ручном запуске), строка не
позже срока (конверт с K от основы)."""
from __future__ import annotations

import json
import select
import sys
import time

from awgbot.infra.rsvproc.status import OpenError


def _read_input() -> dict:
    """Первая строка stdin — вход. Не разобралась как строка (JSON в несколько
    строк при ручном запуске) — дочитать до конца и разобрать целиком."""
    raw = sys.stdin.readline()
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raw += sys.stdin.read()
        try:
            data = json.loads(raw or "{}")
        except ValueError as e:
            raise OpenError(f"вход не JSON: {e}") from e
    if not isinstance(data, dict):
        raise OpenError("вход не объект")
    return data


def _read_line_within(budget_s: float) -> str:
    """Строка из stdin не позже срока; пусто — EOF (бот закрыл трубу) или срок."""
    deadline = time.monotonic() + budget_s
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return ""
        ready, _w, _x = select.select([sys.stdin], [], [], min(left, 5.0))
        if ready:
            return sys.stdin.readline()

