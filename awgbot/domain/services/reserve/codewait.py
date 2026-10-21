"""Итог аварийного кода — прямо в ответе на сообщение (Т-РН3).

Конверт ключа код снимает за секунды: ждём первый шаг процесса открытия за
конвертом и отвечаем «принят» или «не подошёл» сразу; не успел — итог придёт
уведомлением (`reserve_code_failed`). Отдельный модуль: модуль голосования не
читает таймеров ожидания.
"""
from __future__ import annotations

import time

VERDICT_S = 8.0                   # сколько ждать итог кода в ответе на сообщение
POLL_S = 0.25


def verdict(timeout: float | None = None) -> str:
    """«ok» — ключ собран (шаг store и дальше), «fail» — отказ, «» — не успел."""
    from awgbot.infra import rsvzone
    deadline = time.monotonic() + (VERDICT_S if timeout is None else timeout)
    while True:
        step = str(rsvzone.open_status().get("step") or "")
        if step == "fail":
            return "fail"
        if step and step not in ("launching", "launched"):
            return "ok"
        if time.monotonic() >= deadline:
            return ""
        time.sleep(POLL_S)
