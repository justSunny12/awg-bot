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
                     traffic_tx_period = traffic_tx_period + COALESCE((SELECT traffic_tx_period FROM device_traffic WHERE device_id = ?), 0),
                     rf_rx_month       = rf_rx_month       + COALESCE((SELECT rf_rx_month       FROM device_traffic WHERE device_id = ?), 0),
                     rf_tx_month       = rf_tx_month       + COALESCE((SELECT rf_tx_month       FROM device_traffic WHERE device_id = ?), 0)
                   WHERE device_id = ?""",
                (src_device_id, src_device_id, src_device_id, src_device_id,
                 src_device_id, src_device_id, dst_device_id),
            )

    def reset_month_traffic_all(self) -> None:
        """Сброс месячных счётчиков у всех устройств (1-го числа 00:00 UTC+3)."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE device_traffic SET traffic_rx_month = 0, traffic_tx_month = 0, "
                "rf_rx_month = 0, rf_tx_month = 0"
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
               WHERE d.client_id = ? AND NOT EXISTS
                 (SELECT 1 FROM gateways g WHERE g.device_id = d.id
                     OR (d.twin_of IS NOT NULL AND g.device_id = d.twin_of))""",
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

    # ── учёт РФ-трафика (концепт «учёт РФ-трафика») ──────────────────────────

    _NOT_GATEWAY_SQL = """NOT EXISTS
                 (SELECT 1 FROM gateways g WHERE g.device_id = d.id
                     OR (d.twin_of IS NOT NULL AND g.device_id = d.twin_of))"""

    def rf_acct_devices(self) -> list[tuple[int, str]]:
        """[(device_id, address)] — все устройства, кроме шлюзов и их двойников:
        им счётчики в ядре; кто из них имеет доступ к функции, решает интерфейс."""
        return [(int(r["id"]), str(r["address"] or "")) for r in self._connection().execute(
            f"SELECT d.id, d.address FROM devices d WHERE {self._NOT_GATEWAY_SQL} ORDER BY d.id")]

    def rf_samples_all(self) -> dict[int, tuple[int, int]]:
        return {int(r["device_id"]): (int(r["last_up"]), int(r["last_dn"])) for r in
                self._connection().execute("SELECT device_id, last_up, last_dn FROM rf_samples")}

    def rf_set_samples(self, rows: list[tuple[int, int, int]]) -> None:
        if not rows:
            return
        now = _now_iso()
        with self._tx() as cur:
            cur.executemany(
                """INSERT INTO rf_samples (device_id, last_up, last_dn, last_update)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     last_up = excluded.last_up, last_dn = excluded.last_dn,
                     last_update = excluded.last_update""",
                [(d, up, dn, now) for d, up, dn in rows])

    def rf_add_bulk(self, rows: list[tuple[int, int, int]]) -> None:
        """Батч дельт РФ: [(device_id, d_up, d_dn)]; ↑ устройства — rf_rx, ↓ — rf_tx."""
        if not rows:
            return
        with self._tx() as cur:
            cur.executemany(
                """UPDATE device_traffic SET rf_rx_month = rf_rx_month + ?, rf_tx_month = rf_tx_month + ?
                   WHERE device_id = ?""",
                [(up, dn, d) for d, up, dn in rows])

    def get_client_rf(self, client_id: int) -> dict[str, int]:
        """РФ профиля за месяц — сумма по устройствам владельца без шлюзов."""
        row = self._connection().execute(
            f"""SELECT COALESCE(SUM(t.rf_rx_month), 0) AS rx, COALESCE(SUM(t.rf_tx_month), 0) AS tx
                FROM device_traffic t JOIN devices d ON d.id = t.device_id
                WHERE d.client_id = ? AND {self._NOT_GATEWAY_SQL}""", (client_id,)).fetchone()
        return dict(row)

    def rf_by_client(self) -> dict[int, tuple[int, int]]:
        """client_id → (rx, tx) РФ за месяц одним GROUP BY, без шлюзов."""
        return {int(r["client_id"]): (int(r["rx"]), int(r["tx"])) for r in self._connection().execute(
            f"""SELECT d.client_id, COALESCE(SUM(t.rf_rx_month), 0) AS rx,
                       COALESCE(SUM(t.rf_tx_month), 0) AS tx
                FROM device_traffic t JOIN devices d ON d.id = t.device_id
                WHERE {self._NOT_GATEWAY_SQL} GROUP BY d.client_id""")}

    def get_total_month_rf(self) -> dict[str, int]:
        """Сумма РФ по всем устройствам (для сверки с итогом сервера: «вне профилей»)."""
        row = self._connection().execute(
            "SELECT COALESCE(SUM(rf_rx_month), 0) AS rx, COALESCE(SUM(rf_tx_month), 0) AS tx "
            "FROM device_traffic").fetchone()
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
