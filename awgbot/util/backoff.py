"""
backoff.py — разброс сроков сетевых задач: повтор, проба и опрос не по
расписанию (строго периодический запрос в сети выглядит маячком).

`jitter(base, frac)` — base ± frac равномерно (по умолчанию ± 40 %), один
вызов генератора: `random.uniform(1 − frac, 1 + frac)` (его подменяют тесты);
`rnd` — свой источник [0, 1) (края разброса в тестах). Тот же `jitter` — у
кворума (util/quorum/timing).
"""
from __future__ import annotations

import random


def jitter(base: float, frac: float = 0.4, rnd=None) -> float:
    """base ± frac (равномерно): интервал пробы, повтор опроса — не по расписанию."""
    if rnd is None:
        return float(base) * random.uniform(1.0 - frac, 1.0 + frac)
    return float(base) * (1.0 - frac + 2.0 * frac * rnd())
