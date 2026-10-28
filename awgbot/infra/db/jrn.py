"""jrn.py — эпоха линии данных (журнал данных резерва): чтение и переходы роли."""

from __future__ import annotations

from awgbot.util.timeutil import now_iso as _now_iso

_COLS = ("epoch", "holder", "role", "changed_at", "why")


class JrnMixin:

    def jrn_meta(self) -> dict:
        row = self._connection().execute("SELECT " + ", ".join(_COLS) + " FROM jrn_meta WHERE id = 1").fetchone()
        if row is None:
            return {"epoch": 0, "holder": "self", "role": "holder", "changed_at": "", "why": ""}
        return dict(zip(_COLS, row))

    def jrn_following(self) -> bool:
        return self.jrn_meta()["role"] != "holder"

    def jrn_pass(self, *, holder: str, role: str, why: str) -> dict | None:
        """Передача права записи: эпоха + 1, держатель и роль. Уже так — None.
        Коммит с synchronous = FULL: переход редок, а потерять его при пропаже
        питания нельзя — на диске осталась бы прежняя роль."""
        cur_meta = self.jrn_meta()
        if cur_meta["holder"] == holder and cur_meta["role"] == role:
            return None
        conn = self._connection()
        conn.execute("PRAGMA synchronous = FULL")
        try:
            with self._tx() as c:
                c.execute("INSERT OR IGNORE INTO jrn_meta (id) VALUES (1)")
                c.execute("UPDATE jrn_meta SET epoch = epoch + 1, holder = ?, role = ?, changed_at = ?, why = ? "
                          "WHERE id = 1", (holder, role, _now_iso(), why))
                self._tx_note(self._TX_COUNTERS)          # переход эпохи — не данные пользователей: пакет не собирать
        finally:
            conn.execute("PRAGMA synchronous = NORMAL")
        return self.jrn_meta()

    # ── журнал: операции держателя над счётчиками и дельты ──────────────────
    # Запись счётчиков опросом триггер не отличит от сброса или сложения пары —
    # эти три метода пишут событие сами, в своей транзакции (cur — её курсор).

    def _jrn_op(self, cur, op: str, args: dict) -> None:
        """Сброс месяца, сброс периода, сложение пары переезда — решения держателя:
        у следующего — отказ, у держателя при журнале — событие той же транзакцией."""
        import json
        import sqlite3
        from awgbot.util import rsvjrn
        row = cur.execute("SELECT role, applying FROM jrn_meta WHERE id = 1").fetchone()
        if row is not None and row[0] != "holder" and not row[1]:
            raise sqlite3.IntegrityError(f"{rsvjrn.REFUSAL_PREFIX} эпоха не у этого процесса — решения пишет держатель")
        cur.execute("INSERT INTO jrn_events (e, t, op, k, at) SELECT epoch, 'device_traffic', ?, ?, "
                    "strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime') FROM jrn_meta "
                    "WHERE id = 1 AND capture = 1 AND applying = 0", (op, json.dumps(args, sort_keys=True)))

    def _jrn_counts(self, cur, rows: list[tuple]) -> None:
        """Дельты счётчиков этого хоста — в jrn_counts той же транзакцией (при журнале,
        не при применении чужих): [(device_id, rx, tx, rf_rx, rf_tx, hs)]. Метка
        месяца — месяц последнего сброса (`last_monthly_reset`), а не календарь:
        дельта, снятая до сброса, у получателя уйдёт в архив того месяца, а не в
        счётчики после сброса (сброс бывает и не 1-го числа)."""
        if not rows:
            return
        from awgbot.util import timeutil
        month = timeutil.now().strftime("%Y-%m")
        cur.executemany("INSERT INTO jrn_counts (month, device_id, rx, tx, rf_rx, rf_tx, hs) "
                        "SELECT COALESCE((SELECT value FROM server_state WHERE key = 'last_monthly_reset'), ?), "
                        "?, ?, ?, ?, ?, ? FROM jrn_meta WHERE id = 1 AND capture = 1 AND applying = 0",
                        [(month, *r) for r in rows])
