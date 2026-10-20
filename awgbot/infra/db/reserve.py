"""reserve.py — резерв: строка пары и месячный итог трафика через резерв."""

from __future__ import annotations

from typing import Optional

from awgbot.util.timeutil import now_iso as _now_iso


class ReserveMixin:

    # ── пара ─────────────────────────────────────────────────────────────────

    _PEER_COLS = ("id", "name", "host", "pair_if", "pair_port", "peer_port", "pair_cidr", "listener",
                  "bot_username", "peer_admin_id", "created_at")

    def reserve_peer(self) -> Optional[dict]:
        """Пара (в первой версии — одна строка) или None."""
        row = self._connection().execute("SELECT " + ", ".join(self._PEER_COLS) + " FROM reserve_peer LIMIT 1").fetchone()
        return dict(zip(self._PEER_COLS, row)) if row else None

    def reserve_peer_set(self, *, name: str, host: str, pair_if: str, pair_port: int, peer_port: int,
                         pair_cidr: str, listener: bool, bot_username: str = "", peer_admin_id: int = 0) -> int:
        """Создать или заменить строку пары (одна на сервер)."""
        with self._tx() as c:
            c.execute("DELETE FROM reserve_peer")
            cur = c.execute(
                "INSERT INTO reserve_peer (name, host, pair_if, pair_port, peer_port, pair_cidr, listener, "
                "bot_username, peer_admin_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, host, pair_if, int(pair_port), int(peer_port), pair_cidr, 1 if listener else 0,
                 bot_username, int(peer_admin_id), _now_iso()))
            return int(cur.lastrowid)

    def reserve_peer_update(self, **fields) -> None:
        """Точечно: имя, адрес, @бот, admin_id другой стороны."""
        allowed = {k: v for k, v in fields.items() if k in ("name", "host", "bot_username", "peer_admin_id", "peer_port")}
        if not allowed:
            return
        sets = ", ".join(f"{k} = ?" for k in allowed)
        with self._tx() as c:
            c.execute(f"UPDATE reserve_peer SET {sets}", tuple(allowed.values()))

    def reserve_peer_delete(self) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM reserve_peer")

    # ── месячный итог трафика через резерв ───────────────────────────────────

    def reserve_traffic_archive(self, month: str, direction: str, rx: int, tx: int) -> None:
        """Закрыть месяц: одна строка на месяц и направление (`out` — мои у
        резерва, `in` — его у меня); повтор — замена."""
        with self._tx() as c:
            c.execute("DELETE FROM reserve_traffic_monthly WHERE month = ? AND dir = ?", (month, direction))
            c.execute("INSERT INTO reserve_traffic_monthly (month, dir, rx, tx, archived_at) VALUES (?, ?, ?, ?, ?)",
                      (month, direction, int(rx), int(tx), _now_iso()))

    def reserve_traffic_months(self, limit: int = 12) -> list[dict]:
        rows = self._connection().execute(
            "SELECT month, dir, rx, tx, archived_at FROM reserve_traffic_monthly "
            "ORDER BY month DESC, dir LIMIT ?", (int(limit),)).fetchall()
        return [dict(zip(("month", "dir", "rx", "tx", "archived_at"), r)) for r in rows]
