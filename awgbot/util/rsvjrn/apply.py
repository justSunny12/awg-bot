"""
apply.py — часть пакета rsvjrn (см. __init__): применение событий держателя к
базе следующего (основа в отъезде, процесс открытия у резерва).

Одна транзакция на пачку: `applying = 1` в начале и `0` в конце той же
транзакции — триггеры запрета и захвата молчат только для применителя
(другие соединения отметки не видят). Событие записывается в `jrn_events`
самим применителем, с номером и эпохой держателя; `applied` — последний
применённый номер. Номер не больше `applied` — повтор, пропуск (идемпотентно).

Событие — физический образ строки: колонки, которой у применителя нет,
отбрасываются; таблица, которой нет, — в отчёт «не встало». Операции над
счётчиками (`month`, `period`, `tmerge`) повторяются той же SQL, что у
держателя (infra/db/traffic).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field


@dataclass
class Event:
    i: int
    e: int
    t: str
    op: str
    k: dict
    c: dict | None = None
    o: dict | None = None
    at: str = ""

    @classmethod
    def from_row(cls, row) -> "Event":
        i, e, t, op, k, c, o, at = row
        return cls(int(i), int(e), str(t), str(op), json.loads(k or "{}"),
                   json.loads(c) if c else None, json.loads(o) if o else None, str(at or ""))

    def row(self) -> tuple:
        return (self.i, self.e, self.t, self.op, json.dumps(self.k, ensure_ascii=False, sort_keys=True),
                json.dumps(self.c, ensure_ascii=False) if self.c is not None else None,
                json.dumps(self.o, ensure_ascii=False) if self.o is not None else None, self.at)


@dataclass
class Applied:
    """Итог пачки: последний номер, применённые, пропущенные повторы, не вставшие (таблица или ключ)."""
    last: int = 0
    done: list[Event] = field(default_factory=list)
    skipped: int = 0
    lost: list[tuple[Event, str]] = field(default_factory=list)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA main.table_info({_q(table)})")]


def _where(k: dict) -> tuple[str, list]:
    if not k:
        return "", []
    return " WHERE " + " AND ".join(f"{_q(c)} IS ?" for c in k), list(k.values())


def _drop_null_secrets(table: str, vals: dict) -> dict:
    """Пустой секрет в событии — «не трогать»: у бота на резерве закрытых ключей основы нет."""
    from .classes import SECRET_COLUMNS
    sec = SECRET_COLUMNS.get(table, ())
    return {c: v for c, v in vals.items() if not (c in sec and v is None)}


def _row(conn, ev: Event, cols: list[str]) -> None:
    have = set(cols)
    k = {c: v for c, v in ev.k.items() if c in have}
    if ev.op == "I":
        vals = {**{c: v for c, v in (ev.c or {}).items() if c in have}, **k}
        where, args = _where(k)
        if k and conn.execute(f"SELECT 1 FROM main.{_q(ev.t)}{where}", args).fetchone():
            vals = _drop_null_secrets(ev.t, vals)
            if vals:                                       # строка уже есть (снимок её нёс) — значения события
                sets = ", ".join(f"{_q(c)} = ?" for c in vals)
                conn.execute(f"UPDATE main.{_q(ev.t)} SET {sets}{where}", list(vals.values()) + args)
            return
        names = ", ".join(_q(c) for c in vals)
        marks = ", ".join("?" for _ in vals)
        conn.execute(f"INSERT INTO main.{_q(ev.t)} ({names}) VALUES ({marks})", list(vals.values()))
    elif ev.op == "U":
        vals = _drop_null_secrets(ev.t, {c: v for c, v in (ev.c or {}).items() if c in have})
        if not vals:
            return
        where, args = _where(k)
        sets = ", ".join(f"{_q(c)} = ?" for c in vals)
        conn.execute(f"UPDATE main.{_q(ev.t)} SET {sets}{where}", list(vals.values()) + args)
    elif ev.op == "D":
        where, args = _where(k)
        if where:
            conn.execute(f"DELETE FROM main.{_q(ev.t)}{where}", args)


def _counter_op(conn, ev: Event) -> None:
    """Операции держателя над счётчиками — той же SQL, что у infra/db/traffic."""
    if ev.op == "month":
        conn.execute("UPDATE device_traffic SET traffic_rx_month = 0, traffic_tx_month = 0, "
                     "rf_rx_month = 0, rf_tx_month = 0")
        if ev.k.get("month"):                              # метка дельт счётчиков — по сбросу держателя
            conn.execute("INSERT INTO server_state (key, value) VALUES ('last_monthly_reset', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(ev.k["month"]),))
    elif ev.op == "period":
        conn.execute("UPDATE device_traffic SET traffic_rx_period = 0, traffic_tx_period = 0 "
                     "WHERE device_id IN (SELECT id FROM devices WHERE client_id = ?)", (int(ev.k["client_id"]),))
    elif ev.op == "tmerge":
        src, dst = int(ev.k["src"]), int(ev.k["dst"])
        cols = ("traffic_rx_month", "traffic_tx_month", "traffic_rx_period", "traffic_tx_period",
                "rf_rx_month", "rf_tx_month")
        sets = ", ".join(f"{c} = {c} + COALESCE((SELECT {c} FROM device_traffic WHERE device_id = ?), 0)" for c in cols)
        conn.execute(f"UPDATE device_traffic SET {sets} WHERE device_id = ?", [src] * len(cols) + [dst])


COUNTER_OPS = ("month", "period", "tmerge")


def apply(conn: sqlite3.Connection, events: list[Event]) -> Applied:
    """Применить события по номеру одной транзакцией (commit/rollback — здесь же).
    Соединение — с триггерами (DatabaseCore) или сырое: отметка applying глушит их."""
    out = Applied()
    cols_cache: dict[str, list[str]] = {}
    conn.execute("BEGIN IMMEDIATE")                      # номер и схема — внутри: два применителя не применят одно дважды
    try:
        meta = conn.execute("SELECT applied FROM main.jrn_meta WHERE id = 1").fetchone()
        last = int(meta[0]) if meta else 0
        out.last = last
        tables = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        conn.execute("UPDATE main.jrn_meta SET applying = 1 WHERE id = 1")
        for ev in sorted(events, key=lambda x: x.i):
            if ev.i <= last:
                out.skipped += 1
                continue
            if ev.op in COUNTER_OPS:
                _counter_op(conn, ev)
            elif ev.t not in tables:
                out.lost.append((ev, "таблицы нет"))
            else:
                if ev.t not in cols_cache:
                    cols_cache[ev.t] = _columns(conn, ev.t)
                try:
                    conn.execute("SAVEPOINT jrn_ev")
                    _row(conn, ev, cols_cache[ev.t])
                    conn.execute("RELEASE jrn_ev")
                except sqlite3.IntegrityError as e:          # уникальность или связь — не встало, остальное идёт
                    conn.execute("ROLLBACK TO jrn_ev")
                    conn.execute("RELEASE jrn_ev")
                    out.lost.append((ev, str(e)))
            conn.execute("INSERT OR REPLACE INTO main.jrn_events (i, e, t, op, k, c, o, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         ev.row())
            last = ev.i
            out.done.append(ev)
        conn.execute("UPDATE main.jrn_meta SET applying = 0, applied = ? WHERE id = 1", (last,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    out.last = last
    return out


def undo(conn: sqlite3.Connection, events: list[Event]) -> list[tuple[Event, str]]:
    """Откатить свои события (хвост старой эпохи) в базе по прежним значениям — в уже
    открытой транзакции с applying = 1 (зовущий ставит и снимает): база — снова на
    точке расхождения, события держателя лягут без столкновений, а хвост вернётся
    после перебазирования его событиями. Сбросы и сложения счётчиков не откатываются
    (они не решения о строках). Не откатившееся — в ответ (строкой журнала)."""
    lost: list[tuple[Event, str]] = []
    cols_cache: dict[str, list[str]] = {}
    from .rebase import NOT_TAIL
    for ev in sorted(events, key=lambda x: x.i, reverse=True):
        if ev.op in COUNTER_OPS or ev.t in NOT_TAIL:
            continue                                     # данные хоста держатель не вернёт — не откатывать
        if ev.t not in cols_cache:
            cols_cache[ev.t] = _columns(conn, ev.t)
        have = set(cols_cache[ev.t])
        if not have:
            continue
        k = {c: v for c, v in ev.k.items() if c in have}
        where, args = _where(k)
        try:
            conn.execute("SAVEPOINT jrn_undo")
            if ev.op == "U" and where:
                back = {c: v for c, v in (ev.o or {}).items() if c in have and c not in k
                        and (ev.c or {}).get(c) != v}
                if back:
                    sets = ", ".join(f"{_q(c)} = ?" for c in back)
                    conn.execute(f"UPDATE main.{_q(ev.t)} SET {sets}{where}", list(back.values()) + args)
            elif ev.op == "I" and where:
                conn.execute(f"DELETE FROM main.{_q(ev.t)}{where}", args)
            elif ev.op == "D":
                vals = {**{c: v for c, v in (ev.o or {}).items() if c in have}, **k}
                if vals and not (where and conn.execute(f"SELECT 1 FROM main.{_q(ev.t)}{where}", args).fetchone()):
                    names = ", ".join(_q(c) for c in vals)
                    conn.execute(f"INSERT INTO main.{_q(ev.t)} ({names}) VALUES ({', '.join('?' for _ in vals)})",
                                 list(vals.values()))
            conn.execute("RELEASE jrn_undo")
        except sqlite3.Error as e:
            conn.execute("ROLLBACK TO jrn_undo")
            conn.execute("RELEASE jrn_undo")
            lost.append((ev, str(e)))
    return lost


def events_after(conn: sqlite3.Connection, after: int) -> list[Event]:
    """Свои события с номером больше after — по порядку (отправка, хвост)."""
    return [Event.from_row(r) for r in conn.execute(
        "SELECT i, e, t, op, k, c, o, at FROM main.jrn_events WHERE i > ? ORDER BY i", (int(after),))]
