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
