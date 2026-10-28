"""
counts.py — часть пакета rsvjrn (см. __init__): поток счётчиков.

Трафик, РФ-трафик и последнее рукопожатие — измерения, а не решения: каждая
сторона шлёт свои дельты, получатель их складывает (рукопожатие — максимум),
эпоха их не ограждает, хвоста у них не бывает. Пачка — свёртка строк
`jrn_counts` ровно с s0 + 1 по s1 по месяцу и устройству; номер пачки `bn` —
счётчик пачек хоста (он и в открытом заголовке: номер строки выдавал бы
резерву, сколько раз устройства были активны). Получатель принимает номер
больше последнего принятого от этого хоста (`jrn_seen`) — повтор
отбрасывается; отправитель держит одну неподтверждённую пачку и повторяет
её той же (те же строки), новую — только после подтверждения.
Номер хоста — линия и сторона («…:a» основа, «…:b» клон на резерве): стороны
меряют разные интерфейсы, их дельты не пересекаются. Месяц пачки старше
текущего — в архив того месяца (traffic_monthly), а не в счётчики.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class Batch:
    host: str
    bn: int                                           # номер пачки хоста (подряд по одной)
    s0: int = 0                                       # строки jrn_counts: с s0 + 1 …
    s1: int = 0                                       # … по s1 включительно
    months: dict[str, dict[int, list]] = field(default_factory=dict)   # месяц → устройство → [rx, tx, rf_rx, rf_tx, hs]

    def as_json(self) -> dict:
        return {"v": 1, "host": self.host, "bn": self.bn, "s0": self.s0, "s1": self.s1,
                "m": {m: {str(d): v for d, v in devs.items()} for m, devs in self.months.items()}}

    @classmethod
    def from_json(cls, data: dict) -> "Batch":
        months = {str(m): {int(d): list(v) for d, v in (devs or {}).items()} for m, devs in (data.get("m") or {}).items()}
        return cls(host=str(data["host"]), bn=int(data["bn"]), s0=int(data.get("s0") or 0), s1=int(data.get("s1") or 0),
                   months=months)


def fold(conn: sqlite3.Connection, host: str, after: int = 0, upto: int | None = None, bn: int = 0) -> Batch | None:
    """Свёртка своих дельт с номером строки больше after (и не больше upto); нечего — None."""
    sql, args = "SELECT s, month, device_id, rx, tx, rf_rx, rf_tx, hs FROM main.jrn_counts WHERE s > ?", [int(after)]
    if upto is not None:
        sql, args = sql + " AND s <= ?", args + [int(upto)]
    rows = conn.execute(sql + " ORDER BY s", args).fetchall()
    if not rows:
        return None
    b = Batch(host=host, bn=int(bn), s0=int(after), s1=int(rows[-1][0]))
    for _s, month, dev, rx, tx, rf_rx, rf_tx, hs in rows:
        acc = b.months.setdefault(str(month), {}).setdefault(int(dev), [0, 0, 0, 0, None])
        acc[0] += int(rx)
        acc[1] += int(tx)
        acc[2] += int(rf_rx)
        acc[3] += int(rf_tx)
        if hs is not None and (acc[4] is None or int(hs) > int(acc[4])):
            acc[4] = int(hs)
    return b


def drop_sent(conn: sqlite3.Connection, upto: int) -> None:
    """Подтверждённые получателем строки — прочь (свёртка их больше не повезёт)."""
    conn.execute("DELETE FROM main.jrn_counts WHERE s <= ?", (int(upto),))


def current_month(conn: sqlite3.Connection) -> str:
    """Месяц счётчиков получателя — его последний сброс (как метит дельты отправитель)."""
    from awgbot.util import timeutil
    row = conn.execute("SELECT value FROM main.server_state WHERE key = 'last_monthly_reset'").fetchone()
    return str(row[0]) if row and row[0] else timeutil.now().strftime("%Y-%m")


def apply(conn: sqlite3.Connection, batch: Batch, month: str = "") -> bool | None:
    """Принять пачку чужого хоста одной транзакцией: месяц пачки = месяц счётчиков
    получателя — в счётчики устройства (месяц и период), старше — в архив того
    месяца и в период (месячный сброс период не трогает); рукопожатие — максимум.
    False — повтор (номер не больше принятого). None — в пачке месяц новее, чем у
    получателя (событие сброса ещё не доехало): не применена и не отмечена —
    отправитель повторит её позже."""
    month = month or current_month(conn)
    if any(m > month for m in batch.months):
        return None
    conn.execute("BEGIN IMMEDIATE")
    seen = conn.execute("SELECT s FROM main.jrn_seen WHERE host = ?", (batch.host,)).fetchone()
    if seen is not None and int(seen[0]) >= batch.bn:
        conn.execute("ROLLBACK")
        return False
    try:
        for m, devs in batch.months.items():
            for dev, (rx, tx, rf_rx, rf_tx, hs) in devs.items():
                if m == month:
                    conn.execute("UPDATE device_traffic SET traffic_rx_month = traffic_rx_month + ?, "
                                 "traffic_tx_month = traffic_tx_month + ?, traffic_rx_period = traffic_rx_period + ?, "
                                 "traffic_tx_period = traffic_tx_period + ?, rf_rx_month = rf_rx_month + ?, "
                                 "rf_tx_month = rf_tx_month + ? WHERE device_id = ?",
                                 (rx, tx, rx, tx, rf_rx, rf_tx, dev))
                elif m < month:
                    _archive(conn, dev, m, rx, tx, rf_rx, rf_tx)
                    conn.execute("UPDATE device_traffic SET traffic_rx_period = traffic_rx_period + ?, "
                                 "traffic_tx_period = traffic_tx_period + ? WHERE device_id = ?", (rx, tx, dev))
                if hs is not None:
                    conn.execute("UPDATE device_traffic SET last_handshake = ? WHERE device_id = ? AND "
                                 "(last_handshake IS NULL OR last_handshake = '' OR CAST(last_handshake AS INTEGER) < ?)",
                                 (int(hs), dev, int(hs)))
        conn.execute("INSERT INTO main.jrn_seen (host, s) VALUES (?, ?) ON CONFLICT(host) DO UPDATE SET s = excluded.s",
                     (batch.host, batch.bn))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


def _archive(conn, dev: int, month: str, rx: int, tx: int, rf_rx: int, rf_tx: int) -> None:
    """Дельты прошлого месяца — в его строку traffic_monthly (как сейчас при слиянии)."""
    from awgbot.util import timeutil
    if not (rx or tx or rf_rx or rf_tx):
        return
    row = conn.execute("SELECT id FROM traffic_monthly WHERE device_id = ? AND month = ?", (dev, month)).fetchone()
    if row is not None:
        conn.execute("UPDATE traffic_monthly SET rx = rx + ?, tx = tx + ?, rf_rx = rf_rx + ?, rf_tx = rf_tx + ? "
                     "WHERE id = ?", (rx, tx, rf_rx, rf_tx, row[0]))
        return
    client = conn.execute("SELECT client_id FROM devices WHERE id = ?", (dev,)).fetchone()
    conn.execute("INSERT INTO traffic_monthly (device_id, client_id, month, rx, tx, rf_rx, rf_tx, archived_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (dev, client[0] if client else None, month, rx, tx, rf_rx, rf_tx, timeutil.now_iso()))
