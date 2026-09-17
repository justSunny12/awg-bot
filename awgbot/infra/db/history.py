"""history.py — архивация в *_histories (аудит): снимки эпизодов и сущностей
перед изменением или удалением, помесячные снимки трафика, очистка по ретеншну.
"""

from __future__ import annotations

from awgbot.util.timeutil import now_iso as _now_iso  # единый источник времени (UTC+3)

from .schema import HISTORY_TABLES


class HistoryMixin:

    # ── Архивация в историю (аудит) ──────────────────────────────────────────
    # Явные типизированные методы: статичный SQL, читают активную строку → пишут
    # в *_histories с метаполями archived_at/close_reason → удаляют активную (для
    # эпизодов). Всё в переданном курсоре — вызывающий оборачивает в транзакцию,
    # чтобы переезд INSERT→DELETE был атомарным. reason — причина закрытия.

    def snapshot_pause(self, client_id: int, reason: str, cur=None) -> None:
        """Снять СНИМОК текущего эпизода паузы в историю, НЕ удаляя активную строку
        (used_days — свойство периода, живёт в client_pause до сброса периода).
        Вызывается при выходе из паузы: эпизод в аудит, счётчик остаётся."""
        if cur is None:
            with self._tx() as c:
                return self.snapshot_pause(client_id, reason, c)

        row = cur.execute("SELECT * FROM client_pause WHERE client_id = ?",
                          (client_id,)).fetchone()
        if row is None or not row["pause_active_since"]:
            return
        cur.execute(
            """INSERT INTO client_pause_histories
               (client_id, pause_active_since, pause_reserved_days, pause_used_days,
                pause_balance_days, pause_mode, pause_saved_end, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (client_id, row["pause_active_since"], row["pause_reserved_days"],
             row["pause_used_days"], row["pause_balance_days"], row["pause_mode"],
             row["pause_saved_end"], _now_iso(), reason))

    def archive_pause(self, client_id: int, reason: str, cur=None) -> None:
        """Полностью закрыть паузу: снимок эпизода → историю + удалить активную
        строку (сброс used_days). Вызывается при СБРОСЕ ПЕРИОДА (счётчик обнуляется)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_pause(client_id, reason, c)
        self.snapshot_pause(client_id, reason, cur)
        cur.execute("DELETE FROM client_pause WHERE client_id = ?", (client_id,))

    def archive_grace(self, client_id: int, reason: str, cur=None) -> None:
        """Закрыть эпизод отсрочки: перенести строку client_grace → историю."""
        if cur is None:
            with self._tx() as c:
                return self.archive_grace(client_id, reason, c)

        row = cur.execute("SELECT * FROM client_grace WHERE client_id = ?",
                           (client_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO client_grace_histories
               (client_id, grace_used, grace_pending_cut, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?)""",
            (client_id, row["grace_used"], row["grace_pending_cut"], _now_iso(), reason))
        cur.execute("DELETE FROM client_grace WHERE client_id = ?", (client_id,))

    def archive_subscription(self, client_id: int, reason: str, cur=None) -> None:
        """Снять СНИМОК текущей подписки в историю (строку client_subscription НЕ
        удаляем — подписка 1:1 всегда есть, меняется на новый период на месте)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_subscription(client_id, reason, c)

        row = cur.execute("SELECT * FROM client_subscription WHERE client_id = ?",
                          (client_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO client_subscription_histories
               (client_id, period_start, period_end, period_kind, status,
                archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (client_id, row["period_start"], row["period_end"], row["period_kind"],
             row["status"], _now_iso(), reason))

    def archive_quota(self, client_id: int, reason: str, cur=None) -> None:
        """Снять СНИМОК текущей квоты в историю (строку client_quota НЕ удаляем)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_quota(client_id, reason, c)

        row = cur.execute("SELECT * FROM client_quota WHERE client_id = ?",
                          (client_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO client_quota_histories
               (client_id, traffic_limit, bonus_bytes, bonus_granted_month,
                archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (client_id, row["traffic_limit"], row["bonus_bytes"],
             row["bonus_granted_month"], _now_iso(), reason))

    def archive_device_quota(self, device_id: int, reason: str, cur=None) -> None:
        """Снять СНИМОК текущего лимита устройства в историю (перед изменением).
        Активную строку device_traffic не трогаем."""
        if cur is None:
            with self._tx() as c:
                return self.archive_device_quota(device_id, reason, c)
        row = cur.execute(
            "SELECT t.traffic_limit, d.client_id FROM device_traffic t "
            "JOIN devices d ON d.id = t.device_id WHERE t.device_id = ?",
            (device_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO device_quota_histories
               (device_id, client_id, traffic_limit, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?)""",
            (device_id, row["client_id"], row["traffic_limit"], _now_iso(), reason))

    def archive_friend(self, device_id: int, reason: str, cur=None) -> None:
        """Закрыть гостевой доступ: перенести строку device_friend → историю.
        client_id владельца берём из devices для сшивки."""
        if cur is None:
            with self._tx() as c:
                return self.archive_friend(device_id, reason, c)

        owner = cur.execute(
            "SELECT d.client_id, h.tg_id AS holder_tg FROM devices d "
            "LEFT JOIN clients h ON h.id = d.holder_client_id WHERE d.id = ?",
            (device_id,)).fetchone()
        row = cur.execute("SELECT * FROM device_friend WHERE device_id = ?",
                          (device_id,)).fetchone()
        if row is not None:
            tg, code, status = None, row["friend_code"], row["friend_status"]
        elif owner is not None and owner["holder_tg"] is not None:
            tg, code, status = owner["holder_tg"], None, "active"    # держатель — эпизод дружбы
        else:
            return
        cur.execute(
            """INSERT INTO device_friend_histories
               (device_id, client_id, friend_tg_id, friend_code, friend_status,
                archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (device_id, owner["client_id"] if owner else None, tg, code, status,
             _now_iso(), reason))
        cur.execute("DELETE FROM device_friend WHERE device_id = ?", (device_id,))

    def archive_block(self, client_id: int, mask: int, reason: str, cur=None) -> None:
        """Записать снятый эпизод блокировки клиента (какая маска была снята)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_block(client_id, mask, reason, c)

        if not mask:
            return
        cur.execute(
            """INSERT INTO client_block_histories
               (client_id, block_reason, archived_at, close_reason)
               VALUES (?, ?, ?, ?)""",
            (client_id, int(mask), _now_iso(), reason))

    def archive_client_snapshot(self, client_id: int, reason: str, cur=None) -> None:
        """Снимок клиента перед удалением (identity на момент смерти)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_client_snapshot(client_id, reason, c)

        row = cur.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO clients_histories
               (client_id, tg_id, name, device_limit, block_reason, is_service,
                created_at, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], row["tg_id"], row["name"], row["device_limit"],
             row["block_reason"], row["is_service"], row["created_at"],
             _now_iso(), reason))

    def archive_device_snapshot(self, device_id: int, reason: str, cur=None) -> None:
        """Снимок устройства перед удалением."""
        if cur is None:
            with self._tx() as c:
                return self.archive_device_snapshot(device_id, reason, c)

        row = cur.execute(
            "SELECT d.*, t.traffic_limit AS t_limit FROM devices d "
            "JOIN device_traffic t ON t.device_id = d.id WHERE d.id = ?",
            (device_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO devices_histories
               (device_id, client_id, name, public_key, address,
                block_reason, traffic_limit, created_at, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], row["client_id"], row["name"],
             row["public_key"], row["address"], row["block_reason"],
             row["t_limit"], row["created_at"], _now_iso(), reason))

    def snapshot_monthly_traffic(self, month: str, cur=None) -> None:
        """Снять помесячный снимок потребления ВСЕХ устройств (перед сбросом 1-го
        числа). month = 'YYYY-MM' завершившегося месяца. Метрика, не эпизод —
        активные счётчики не трогаем (их обнуляет reset_month_traffic_all)."""
        if cur is None:
            with self._tx() as c:
                return self.snapshot_monthly_traffic(month, c)

        rows = cur.execute(
            "SELECT t.device_id, d.client_id, t.traffic_rx_month, t.traffic_tx_month "
            "FROM device_traffic t JOIN devices d ON d.id = t.device_id").fetchall()
        stamp = _now_iso()
        for r in rows:
            if not r["traffic_rx_month"] and not r["traffic_tx_month"]:
                continue          # нулевые месяцы не пишем
            cur.execute(
                """INSERT INTO traffic_monthly
                   (device_id, client_id, month, rx, tx, archived_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["device_id"], r["client_id"], month,
                 r["traffic_rx_month"], r["traffic_tx_month"], stamp))

    def purge_histories(self, cutoff_iso: str, batch_size: int = 500) -> dict[str, int]:
        """Удалить исторические записи старше cutoff (по archived_at) из ВСЕХ
        таблиц реестра HISTORY_TABLES. Дженерик: имена берутся из своего реестра
        (не из пользовательского ввода), SQL по archived_at универсален и
        безопасен. Удаляем БАТЧАМИ (LIMIT в цикле), чтобы не держать долгую
        блокировку на большой истории. Каждый батч — отдельная короткая транзакция.
        Возвращает {таблица: удалено_строк}."""
        removed: dict[str, int] = {}
        for table in HISTORY_TABLES:
            total = 0
            while True:
                with self._tx() as cur:
                    cur.execute(
                        f"DELETE FROM {table} WHERE rowid IN ("
                        f"  SELECT rowid FROM {table} WHERE archived_at < ? LIMIT ?)",
                        (cutoff_iso, batch_size))
                    n = cur.rowcount
                total += n
                if n < batch_size:
                    break
            if total:
                removed[table] = total
        return removed
