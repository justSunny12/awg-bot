"""Улики вместо зонда — одна механика у обеих ролей.

Зонд наружу — строго периодический коннект в одну и ту же цель, то есть
сигнатура. Поэтому сначала улики: вернувшийся через линк трафик клиентов —
прямое доказательство пути, и стоит оно ноль пакетов наружу. Спрос без ответа
(в линк шлют, обратно тихо) — худший случай, зондируем сразу. Не ходит никто —
зонд редкий и с джиттером. Сервер смотрит на линк со своей стороны (ответы —
rx линка), агент — со своей (ответы — tx линка); всё остальное общее.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable


class EvidenceProbe:
    """Состояние одного линка: сырые счётчики (перезапуск интерфейса их
    обнуляет — прежний замер не с чем сравнивать) и последний замер."""

    def __init__(self, threshold: int = 4096):
        self.threshold = int(threshold)
        self._raw: tuple[int, int] | None = None
        self._seen: dict | None = None

    def reset(self) -> None:
        self._raw = None
        self._seen = None

    def verdict(self, rx: int, tx: int, *, minus: Callable[[int, int], tuple[int, int]],
                probe: Callable[[], Any], every: float, on_traffic: Callable[[Any], Any],
                back_is_rx: bool, fresh: bool = True) -> tuple[Any, str]:
        """(результат, источник) — источник: probe | traffic | cache.

        rx, tx — сырые счётчики линка; minus — они же за вычетом собственного
        канала (снимки, ответы и фиды ходят тем же линком и сошли бы за трафик
        клиентов); probe() — зонд, его результат хранится как есть; every —
        секунд между зондами в простое (0 — каждый такт); on_traffic(prev) —
        каким становится результат, когда путь доказан трафиком; back_is_rx —
        по какому счётчику приходят ответы; fresh — улика засчитывается только
        при живом хендшейке (сервер), агенту — всегда."""
        now = time.monotonic()
        last_raw = self._raw
        self._raw = (rx, tx)
        restarted = last_raw is not None and (rx < last_raw[0] or tx < last_raw[1])
        rx, tx = minus(rx, tx)
        seen = self._seen

        def _probe() -> tuple[Any, str]:
            result = probe()
            self._seen = {"rx": rx, "tx": tx, "result": result,
                          "next": now + every * random.uniform(0.6, 1.4)}
            return result, "probe"

        if seen is None or restarted:
            return _probe()
        d_rx, d_tx = rx - seen["rx"], tx - seen["tx"]
        returned = (d_rx if back_is_rx else d_tx) > self.threshold
        demand = (d_tx if back_is_rx else d_rx) > self.threshold
        if returned and fresh:
            # такт зонда отодвигаем, как после зонда: улика — доказательство
            # СИЛЬНЕЕ пробы; иначе первый же тик после конца трафика уходил бы
            # зондом, а конец трафика — обычный вечер, а не отказ
            seen.update(rx=rx, tx=tx, result=on_traffic(seen["result"]),
                        next=now + every * random.uniform(0.6, 1.4))
            return seen["result"], "traffic"
        if demand or every <= 0 or now >= seen.get("next", 0.0):
            return _probe()
        seen.update(rx=rx, tx=tx)
        return seen["result"], "cache"
