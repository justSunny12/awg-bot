"""egress.py — выход наружу через локальный канал."""

from __future__ import annotations

import socket
import time
from awgbot.core import settings
from awgbot.domain.evidence import EvidenceProbe  # noqa: E402


class EgressMixin:
    """Выход наружу через локальный канал."""
    # ── выход наружу через домашний канал ────────────────────────────────────

    def egress_probe(self) -> float | None:
        """TCP-коннект к российскому и к зарубежному адресу через default-маршрут
        (домашний канал, не туннель). Возвращает время первого удачного, мс;
        None — никто не ответил. Две цели: один внешний хост сам по себе точка
        отказа, и его заминка выглядела бы как отвал канала."""
        targets = [str(t) for t in (settings.get("app.gateway.egress_targets", None)
                                    or ["77.88.8.8", "8.8.8.8"])]
        port = int(settings.get("app.gateway.egress_port", 53))
        if len(targets) == 1:
            return self._egress_one(targets[0], port)
        # Цели независимы — ходим ко всем разом, первый ответ и есть результат:
        # последовательно при лежащем канале тик держал поток 2 × 3 с.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        pool = ThreadPoolExecutor(max_workers=len(targets))
        try:
            futs = [pool.submit(self._egress_one, h, port) for h in targets]
            for f in as_completed(futs):
                ms = f.result()
                if ms is not None:
                    return ms
            return None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # Обратный трафик клиентов в линк — улика сильнее зонда: он доказывает,
    # что канал квартиры дошёл до интернета и ответы вернулись. Подделать его
    # служебным нечем: в линк агент шлёт только ответы клиентам да keepalive в
    # 32 байта, свой трафик к Telegram он гонит аплинком.
    _EGRESS_RETURN_BYTES = 4096

    def _egress_idle_seconds(self) -> float:
        """Секунд между зондами наружу, когда через линк не ходит никто;
        0 — зондировать каждый тик (как было)."""
        mins = settings.get_int("app.gateway.monitor_minutes", 3)
        return mins * 60 * float(settings.get("app.gateway.egress_idle_multiplier", 4) or 0)

    def _minus_channel(self, link_rx: int, link_tx: int) -> tuple[int, int]:
        """Счётчики линка за вычетом собственного канала (снимки, ответы,
        фиды): иначе ответ канала серверу сходил бы за ответы из
        интернета, ушедшие клиентам, и выход наружу доказывался бы трафиком,
        который никуда наружу не ходил."""
        return self.channel.minus(0, link_rx, link_tx)

    def _egress_verdict(self, link_rx: int, link_tx: int) -> tuple[bool, float | None, str]:
        """(есть ли выход наружу, мс последнего замера, чем доказано:
        трафик | проба | кэш) — domain/evidence.EvidenceProbe, одна механика с
        сервером. Зонд — коннект с адреса локальной сети к двум фиксированным
        целям, тик за тиком — ровно тот маячок, про который мы сами пишем
        «строго периодический коннект — сигнатура»; поэтому сначала улики:
        вырос tx линка — ответы из интернета дошли и ушли клиентам."""
        ep = self.__dict__.get("_egress_probe")
        if ep is None:
            ep = self.__dict__["_egress_probe"] = EvidenceProbe(self._EGRESS_RETURN_BYTES)

        def _probe() -> tuple[bool, float | None]:
            ms = self.egress_probe()
            return ms is not None, ms

        (ok, ms), src = ep.verdict(link_rx, link_tx, minus=self._minus_channel, probe=_probe,
                                   every=self._egress_idle_seconds(),
                                   on_traffic=lambda prev: (True, prev[1]), back_is_rx=False)
        return ok, ms, {"probe": "проба", "traffic": "трафик", "cache": "кэш"}[src]

    @staticmethod
    def _egress_one(host: str, port: int) -> float | None:
        t0 = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=3.0):
                return round((time.monotonic() - t0) * 1000, 1)
        except OSError:
            return None
