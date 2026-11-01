"""
backoff.py — разброс сроков сетевых задач: повтор, проба и опрос не по
расписанию (строго периодический запрос в сети выглядит маячком).

`jitter(base, frac)` — base ± frac равномерно (по умолчанию ± 40 %), один
вызов генератора: `random.uniform(1 − frac, 1 + frac)` (его подменяют тесты
через этот модуль: `backoff.random`); `rnd` — свой источник [0, 1) (края
разброса в тестах). Тот же `jitter` — у кворума (util/quorum/timing).

`backoff_delay(table, attempt)` — пауза перед попыткой переподключения: шаг
таблицы по номеру попытки (дальше последнего — последний, потолок) с тем же
разбросом. `RECONNECT_S` — таблица каналов пары и гостя (5 с … 5 мин); у
канала линка шлюза — своя, реже на старте (runtime/linkclient).
"""
from __future__ import annotations

import random
from typing import Sequence

RECONNECT_S = (5, 10, 20, 40, 80, 160, 300)


def jitter(base: float, frac: float = 0.4, rnd=None) -> float:
    """base ± frac (равномерно): интервал пробы, повтор опроса — не по расписанию."""
    if rnd is None:
        return float(base) * random.uniform(1.0 - frac, 1.0 + frac)
    return float(base) * (1.0 - frac + 2.0 * frac * rnd())


def backoff_delay(table: Sequence[float], attempt: int, frac: float = 0.4, rnd=None) -> float:
    """Шаг table[attempt] (за краем таблицы — последний) ± frac: пауза перед
    очередной попыткой переподключения."""
    return jitter(table[max(0, min(int(attempt), len(table) - 1))], frac, rnd)


__all__ = ["RECONNECT_S", "jitter", "backoff_delay"]
