"""traffic.py — трафик: накопление и сброс счётчиков, слияние пары переезда,
traffic_samples как база для дельт опросчика.
"""

from __future__ import annotations

from awgbot.util.timeutil import now_iso as _now_iso  # единый источник времени (UTC+3)


class TrafficMixin:

    # ── Трафик: накопление и сброс ───────────────────────────────────────────

    def merge_traffic(self, src_device_id: int, dst_device_id: int) -> None:
        """Сложить счётчики src в dst. Нужен на завершении переезда: потребление
        человека в окне размазано по паре строк, и удаление старой без слияния
        унесло бы половину месяца — молча и в пользу нарушителя лимита.

        Складываем, а не переносим: у двойника уже есть свой накопленный трафик.
        """
        with self._tx() as cur:
            cur.execute(
                """UPDATE device_traffic SET
                     traffic_rx_month  = traffic_rx_month  + COALESCE((SELECT traffic_rx_month  FROM device_traffic WHERE device_id = ?), 0),
                     traffic_tx_month  = traffic_tx_month  + COALESCE((SELECT traffic_tx_month  FROM device_traffic WHERE device_id = ?), 0),
                     traffic_rx_period = traffic_rx_period + COALESCE((SELECT traffic_rx_period FROM device_traffic WHERE device_id = ?), 0),
                     traffic_tx_period = traffic_tx_period + COALESCE((SELECT traffic_tx_period FROM device_traffic WHERE device_id = ?), 0)
                   WHERE device_id = ?""",
                (src_device_id, src_device_id, src_device_id, src_device_id, dst_device_id),
            )

    def reset_month_traffic_all(self) -> None:
        """Сброс месячных счётчиков у всех устройств (1-го числа 00:00 UTC+3)."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE device_traffic SET traffic_rx_month = 0, traffic_tx_month = 0"
            )

    def reset_period_traffic(self, client_id: int) -> None:
        """Сброс ПЕРИОДНЫХ счётчиков устройств клиента (при новом периоде).
        Месячные НЕ трогаем — у них свой цикл."""
        with self._tx() as cur:
            cur.execute(
                """UPDATE device_traffic SET traffic_rx_period = 0, traffic_tx_period = 0
                   WHERE device_id IN (SELECT id FROM devices WHERE client_id = ?)""",
                (client_id,),
            )

    def get_client_traffic(self, client_id: int) -> dict[str, int]:
        """Суммарный трафик клиента по устройствам (месяц + период)."""
        row = self._connection().execute(
            """SELECT
                 COALESCE(SUM(t.traffic_rx_month), 0)  AS rx_month,
                 COALESCE(SUM(t.traffic_tx_month), 0)  AS tx_month,
                 COALESCE(SUM(t.traffic_rx_period), 0) AS rx_period,
                 COALESCE(SUM(t.traffic_tx_period), 0) AS tx_period
               FROM device_traffic t JOIN devices d ON d.id = t.device_id
               WHERE d.client_id = ? AND d.is_gateway = 0 AND NOT EXISTS
                 (SELECT 1 FROM devices o WHERE o.id = d.twin_of AND o.is_gateway = 1)""",
            (client_id,),
        ).fetchone()
        return dict(row)

    def get_total_month_traffic(self) -> dict[str, int]:
        """Суммарное месячное потребление по ВСЕМ устройствам (для админ-панели)."""
        row = self._connection().execute(
            """SELECT COALESCE(SUM(traffic_rx_month), 0) AS rx,
                      COALESCE(SUM(traffic_tx_month), 0) AS tx
               FROM device_traffic"""
        ).fetchone()
        return dict(row)

    # ── traffic_samples: база для дельт ──────────────────────────────────────

    def get_samples_all(self) -> dict[int, tuple[int, int]]:
        """device_id → (last_rx, last_tx) одним запросом — для опроса трафика."""
        return {int(r["device_id"]): (int(r["last_rx"]), int(r["last_tx"])) for r in
                self._connection().execute("SELECT device_id, last_rx, last_tx FROM traffic_samples")}

    def set_samples(self, rows: list[tuple[int, int, int]]) -> None:
        """Батч: [(device_id, last_rx, last_tx)] одним executemany."""
        if not rows:
            return
        now = _now_iso()
        with self._tx() as cur:
            cur.executemany(
                """INSERT INTO traffic_samples (device_id, last_rx, last_tx, last_update)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     last_rx = excluded.last_rx,
                     last_tx = excluded.last_tx,
                     last_update = excluded.last_update""",
                [(d, rx, tx, now) for d, rx, tx in rows],
            )

    def add_traffic_bulk(self, rows: list[tuple[int, int, int]]) -> None:
        """Батч дельт: [(device_id, d_rx, d_tx)] одним executemany."""
        if not rows:
            return
        with self._tx() as cur:
            cur.executemany(
                """UPDATE device_traffic
                   SET traffic_rx_month = traffic_rx_month + ?, traffic_tx_month = traffic_tx_month + ?,
                       traffic_rx_period = traffic_rx_period + ?, traffic_tx_period = traffic_tx_period + ?
                   WHERE device_id = ?""",
                [(rx, tx, rx, tx, d) for d, rx, tx in rows],
            )
