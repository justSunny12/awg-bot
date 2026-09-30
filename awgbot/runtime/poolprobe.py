"""Замер очереди общего пула потоков (asyncio.to_thread) после старта.

Гипотеза rt-18: на 1 vCPU сетевые задачи старта занимают пул, и обработчики
кнопок ждут секунды. Проверяется не на глаз, а замером: первые минуты после
старта раз в пару секунд ставим в пул пустую задачу и меряем, сколько она
ждала очереди; итог — одной строкой в журнал («пул потоков за N с после
старта: …»). Ничего не меняет, наружу не ходит.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("awgbot.poolprobe")

DURATION = 180.0        # с после старта
EVERY = 2.0             # с между замерами
SLOW = 1.0              # с ожидания, которое считаем задержкой


async def measure(duration: float = DURATION, every: float = EVERY) -> dict:
    """{samples, max, avg, slow} — ожидания очереди пула за duration секунд."""
    waits: list[float] = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        await asyncio.to_thread(lambda: None)
        waits.append(time.monotonic() - t0)
        await asyncio.sleep(every)
    if not waits:
        return {"samples": 0, "max": 0.0, "avg": 0.0, "slow": 0}
    return {"samples": len(waits), "max": max(waits), "avg": sum(waits) / len(waits),
            "slow": sum(1 for w in waits if w >= SLOW)}


async def run(tag: str = "") -> None:
    """Фоновая задача старта: замер и одна строка журнала."""
    try:
        st = await measure()
    except Exception as e:                            # noqa: BLE001
        log.warning("%sпул потоков: замер не удался: %s", tag, e)
        return
    log.info("%sпул потоков за %d с после старта: ожидание очереди макс %.2f с, среднее %.3f с, "
             "дольше %.0f с — %d из %d замеров", tag, int(DURATION), st["max"], st["avg"], SLOW,
             st["slow"], st["samples"])
