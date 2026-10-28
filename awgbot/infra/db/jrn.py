"""jrn.py — эпоха линии данных (журнал данных резерва): чтение и переходы роли."""

from __future__ import annotations

from awgbot.util.timeutil import now_iso as _now_iso

_COLS = ("epoch", "holder", "role", "changed_at", "why", "capture", "applied", "acked", "snap_i", "fork_i")
_EMPTY = {"epoch": 0, "holder": "self", "role": "holder", "changed_at": "", "why": "", "capture": 0, "applied": 0,
          "acked": 0, "snap_i": 0, "fork_i": 0}


class JrnMixin:

    def jrn_meta(self) -> dict:
        row = self._connection().execute("SELECT " + ", ".join(_COLS) + " FROM jrn_meta WHERE id = 1").fetchone()
        if row is None:
            return dict(_EMPTY)
        return dict(zip(_COLS, row))

    def jrn_following(self) -> bool:
        return self.jrn_meta()["role"] != "holder"

    def jrn_line(self) -> str:
        """Линия данных этой базы (db_line) — ей помечены события и пачки."""
        return str(self.get_state("db_line") or "")

    def jrn_pass(self, *, holder: str, role: str, why: str, fork_i: int | None = None,
                 epoch: int | None = None, keep_own: bool = False) -> dict | None:
        """Передача права записи: эпоха + 1, держатель и роль. Уже так — None.
        Коммит с synchronous = FULL: переход редок, а потерять его при пропаже
        питания нельзя — на диске осталась бы прежняя роль."""
        cur_meta = self.jrn_meta()
        if cur_meta["holder"] == holder and cur_meta["role"] == role and (epoch is None or cur_meta["epoch"] >= epoch):
            return None
        new_e = max(int(cur_meta["epoch"]) + 1, int(epoch or 0))
        fork = self.jrn_last_i() if fork_i is None else int(fork_i)
        return self._jrn_meta_set(why, epoch=new_e, holder=holder, role=role, fork_i=fork, _keep_own=keep_own)

    def jrn_hand_over(self, *, why: str) -> dict | None:
        """Передача бота кнопкой: если все свои события этой эпохи подтверждены резервом —
        эпоха + 1 второй стороне, здесь следование, точка расхождения — подтверждённое;
        проверка и переход — одной транзакцией (решение из другого потока не вклинится).
        Не всё подтверждено — None."""
        conn = self._connection()
        conn.execute("PRAGMA synchronous = FULL")
        try:
            with self._tx() as c:
                c.execute("UPDATE jrn_meta SET why = why WHERE id = 1")      # замок писателя — до проверки
                row = c.execute("SELECT epoch, acked FROM jrn_meta WHERE id = 1").fetchone()
                if row is None:
                    return None
                epoch, acked = int(row[0]), int(row[1])
                if c.execute("SELECT 1 FROM jrn_events WHERE e = ? AND i > ? LIMIT 1", (epoch, acked)).fetchone():
                    return None
                c.execute("UPDATE jrn_meta SET epoch = ?, holder = 'peer', role = 'follower', fork_i = ?, applied = ?, "
                          "changed_at = ?, why = ? WHERE id = 1", (epoch + 1, acked, acked, _now_iso(), why))
                self._tx_note(self._TX_COUNTERS)
        finally:
            conn.execute("PRAGMA synchronous = NORMAL")
        return self.jrn_meta()

    def jrn_adopt(self, epoch: int, *, why: str, fork_i: int | None = None) -> dict | None:
        """Чужая эпоха больше моей (st.jrn, событие держателя, jstale) — принять её без
        приращения: держатель — вторая сторона, здесь следование. Не больше — None."""
        cur_meta = self.jrn_meta()
        if int(epoch) <= int(cur_meta["epoch"]):
            return None
        fork = self.jrn_last_i() if fork_i is None else int(fork_i)
        return self._jrn_meta_set(why, epoch=int(epoch), holder="peer", role="follower", fork_i=fork)

    def jrn_adopt_epoch(self, epoch: int) -> None:
        """В следовании: номер эпохи — держателя (он открылся сам после аварии, или моя
        выросла сменой роли без событий), роль та же."""
        with self._tx() as c:
            c.execute("UPDATE jrn_meta SET epoch = ? WHERE id = 1 AND role = 'follower' AND holder = 'peer'",
                      (int(epoch),))
            self._tx_note(self._TX_COUNTERS)

    def jrn_seen_reset(self, host: str) -> None:
        """Новый отъезд: счёт пачек клона начинается заново (новый гость)."""
        with self._tx() as c:
            c.execute("DELETE FROM jrn_seen WHERE host = ?", (str(host),))
            self._tx_note(self._TX_COUNTERS)

    def jrn_fence(self, *, why: str) -> dict | None:
        """Держатель без права писать (кворум 4.2.0: лишён большинства) — эпоха та же."""
        cur_meta = self.jrn_meta()
        if cur_meta["holder"] != "self" or cur_meta["role"] == "follower":
            return None
        return self._jrn_meta_set(why, role="follower")

    def jrn_unfence(self, *, why: str) -> dict | None:
        """Ограждение снято (кворум снова есть) — держатель снова пишет, эпоха та же."""
        cur_meta = self.jrn_meta()
        if cur_meta["holder"] != "self" or cur_meta["role"] == "holder":
            return None
        return self._jrn_meta_set(why, role="holder")

    def _jrn_meta_set(self, why: str, **fields) -> dict:
        """Переход эпохи одной транзакцией с synchronous = FULL. Уход в следование от
        чужого держателя: свои события, которых резерв не подтвердил, — в хвост старой
        эпохи (jrn_tail): события держателя ложатся под теми же номерами и затёрли бы их."""
        keep_own = bool(fields.pop("_keep_own", False))
        conn = self._connection()
        conn.execute("PRAGMA synchronous = FULL")
        try:
            with self._tx() as c:
                c.execute("INSERT OR IGNORE INTO jrn_meta (id) VALUES (1)")
                if fields.get("holder") == "peer" and fields.get("role") == "follower" and "fork_i" in fields \
                        and not keep_own:
                    # события держателя пойдут с fork_i + 1: применённое — по точку расхождения
                    fields["applied"] = int(fields["fork_i"])
                if fields.get("holder") == "peer" and fields.get("role") == "follower" and not keep_own:
                    c.execute("INSERT OR REPLACE INTO jrn_tail (i, e, t, op, k, c, o, at, state) "
                              "SELECT i, e, t, op, k, c, o, at, 'wait' FROM jrn_events "
                              "WHERE i > (SELECT acked FROM jrn_meta WHERE id = 1) "
                              "AND e = (SELECT epoch FROM jrn_meta WHERE id = 1)")
                    c.execute("DELETE FROM jrn_events WHERE i > (SELECT acked FROM jrn_meta WHERE id = 1) "
                              "AND e = (SELECT epoch FROM jrn_meta WHERE id = 1)")
                sets = ", ".join(f"{k} = ?" for k in fields)
                c.execute(f"UPDATE jrn_meta SET {sets}, changed_at = ?, why = ? WHERE id = 1",
                          (*fields.values(), _now_iso(), why))
                self._tx_note(self._TX_COUNTERS)          # переход эпохи — не данные пользователей: пакет не собирать
        finally:
            conn.execute("PRAGMA synchronous = NORMAL")
        return self.jrn_meta()

    # ── доставка: свои события и пачки ───────────────────────────────────────

    def jrn_last_i(self) -> int:
        """Последний номер события, когда-либо выданный этой базой (не переиспользуется)."""
        row = self._connection().execute("SELECT seq FROM sqlite_sequence WHERE name = 'jrn_events'").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def jrn_bump(self, upto: int) -> None:
        """Номера событий дальше upto (у резерва есть номера, которых в этой базе уже нет:
        пропало питание или база восстановлена) — следующее событие получит upto + 1."""
        with self._tx() as c:
            if c.execute("SELECT 1 FROM sqlite_sequence WHERE name = 'jrn_events'").fetchone():
                c.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'jrn_events' AND seq < ?",
                          (int(upto), int(upto)))
            else:
                c.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('jrn_events', ?)", (int(upto),))
            self._tx_note(self._TX_COUNTERS)

    def jrn_capture_set(self, on: bool) -> bool:
        """Захват событий включить/выключить; была ли смена."""
        if bool(self.jrn_meta()["capture"]) == bool(on):
            return False
        with self._tx() as c:
            c.execute("UPDATE jrn_meta SET capture = ? WHERE id = 1", (1 if on else 0,))
            if not on:                                    # прежний путь: накопленное не понадобится
                c.execute("DELETE FROM jrn_events")
                c.execute("DELETE FROM jrn_counts")
            self._tx_note(self._TX_COUNTERS)
        return True

    def jrn_events_after(self, after: int, limit: int = 256, epoch: int | None = None) -> list:
        """События с номером больше after (только эпохи epoch, если задана) — по порядку."""
        from awgbot.util.rsvjrn.apply import Event
        sql, args = "SELECT i, e, t, op, k, c, o, at FROM jrn_events WHERE i > ?", [int(after)]
        if epoch is not None:
            sql, args = sql + " AND e = ?", args + [int(epoch)]
        rows = self._connection().execute(sql + " ORDER BY i LIMIT ?", args + [int(limit)]).fetchall()
        return [Event.from_row(tuple(r)) for r in rows]

    def jrn_first_i(self) -> int:
        row = self._connection().execute("SELECT MIN(i) FROM jrn_events").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def jrn_acked_set(self, i: int, *, drop: bool = True) -> None:
        """Резерв подтвердил свои события по i: отметка; подтверждённые — прочь."""
        with self._tx() as c:
            c.execute("UPDATE jrn_meta SET acked = ? WHERE id = 1 AND acked < ?", (int(i), int(i)))
            if drop:
                c.execute("DELETE FROM jrn_events WHERE i <= ?", (int(i),))
            self._tx_note(self._TX_COUNTERS)

    def jrn_snap_set(self, i: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE jrn_meta SET snap_i = ? WHERE id = 1", (int(i),))
            self._tx_note(self._TX_COUNTERS)

    # Пачки счётчиков этого хоста: одна неподтверждённая (cnt_bn > cnt_ack) повторяется той же —
    # те же строки (cnt_s0, cnt_s1]; новая — только после подтверждения. Состояние — в jrn_meta
    # с synchronous = FULL: потеряй его пропажа питания, повтор вышел бы другой пачкой под тем же
    # номером, и получатель счёл бы её повтором.

    def _cnt_meta(self) -> tuple[int, int, int, int]:
        row = self._connection().execute("SELECT cnt_bn, cnt_ack, cnt_s0, cnt_s1 FROM jrn_meta WHERE id = 1").fetchone()
        return tuple(int(x or 0) for x in row) if row else (0, 0, 0, 0)

    def _cnt_set(self, **fields) -> None:
        conn = self._connection()
        conn.execute("PRAGMA synchronous = FULL")
        try:
            with self._tx() as c:
                sets = ", ".join(f"{k} = ?" for k in fields)
                c.execute(f"UPDATE jrn_meta SET {sets} WHERE id = 1", tuple(fields.values()))
                self._tx_note(self._TX_COUNTERS)
        finally:
            conn.execute("PRAGMA synchronous = NORMAL")

    def jrn_counts_out(self, host: str):
        """Пачка к отправке: неподтверждённая — она же; иначе новая из строк после
        последней; нечего — None."""
        from awgbot.util.rsvjrn import counts
        bn, ack, s0, s1 = self._cnt_meta()
        conn = self._connection()
        if bn > ack:
            b = counts.fold(conn, host, s0, s1, bn=bn)
            if b is not None:
                return b
            self._cnt_set(cnt_ack=bn)                         # строк пачки нет (сняты снимком) — подтверждена
        b = counts.fold(conn, host, s1, bn=bn + 1)
        if b is None:
            return None
        self._cnt_set(cnt_bn=b.bn, cnt_s0=b.s0, cnt_s1=b.s1)
        return b

    def jrn_counts_acked(self, bn: int) -> bool:
        """Получатель подтвердил пачку bn: её строки — прочь. Номер не той пачки — нет."""
        cbn, ack, _s0, s1 = self._cnt_meta()
        if int(bn) != cbn or cbn <= ack:
            return False
        with self._tx() as c:
            c.execute("DELETE FROM jrn_counts WHERE s <= ?", (s1,))
            self._tx_note(self._TX_COUNTERS)
        self._cnt_set(cnt_ack=cbn)
        return True

    def jrn_counts_cover(self, cs: int, cb: int) -> None:
        """Снимок принят: строки по cs в нём учтены, пачки по cb — тоже."""
        cbn, ack, s0, s1 = self._cnt_meta()
        with self._tx() as c:
            c.execute("DELETE FROM jrn_counts WHERE s <= ?", (int(cs),))
            self._tx_note(self._TX_COUNTERS)
        fields = {"cnt_s1": max(s1, int(cs))}
        if cbn <= int(cb):
            fields["cnt_ack"] = cbn
        self._cnt_set(**fields)

    def jrn_counts_raise(self, bn: int) -> None:
        """У получателя номер пачек больше моего (моё состояние потеряно: восстановление):
        счёт — от его номера, строки неподтверждённой пачки уйдут следующей."""
        cbn, ack, s0, s1 = self._cnt_meta()
        if int(bn) <= cbn:
            return
        self._cnt_set(cnt_bn=int(bn), cnt_ack=int(bn), cnt_s1=s0 if cbn > ack else s1)

    def jrn_counts_bn(self) -> int:
        return self._cnt_meta()[0]

    # ── применение чужого (следование, гость) ────────────────────────────────

    def jrn_apply(self, events: list):
        """События держателя — одной транзакцией применителя (util/rsvjrn/apply)."""
        from awgbot.util.rsvjrn import apply as _apply
        from awgbot.core import access_cache
        conn = self._connection()
        if conn.in_transaction:
            conn.commit()
        res = _apply.apply(conn, events)
        access_cache.invalidate_all()
        return res

    def jrn_counts_apply(self, batch) -> bool | None:
        from awgbot.util.rsvjrn import counts
        conn = self._connection()
        if conn.in_transaction:
            conn.commit()
        return counts.apply(conn, batch)

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
