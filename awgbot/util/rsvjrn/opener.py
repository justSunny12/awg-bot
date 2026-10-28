"""
opener.py — часть пакета rsvjrn (см. __init__): база бота основы на резерве
по журналу — для процесса открытия (tools/rsvopen).

База гостя = снимок (`pkg3`, его отметки `jrn`) + события основы после
снимка из хранилища резерва (подряд по номерам, пока не разрыв) + журнал
данных гостя (его собственные события, если резерв перезагружался посреди
обслуживания) + пачки счётчиков основы (сторона a) и гостя (b). Применение —
одной транзакцией применителя (apply), сырое соединение. Затем строка эпохи:

- журнал гостя есть — гость продолжает свою эпоху держателем;
- переезд кнопкой — гость следует за основой (её эпоха, держатель — основа),
  пока она не передаст бота;
- аварийный переезд — эпоха основы + 1, гость держатель.

Линия базы гостя — линия основы (его события и пачки помечены ею); месяц
сброса — основы (метка её дельт). Эпоха основы — наибольшая из снимка, её
событий и отметки резерва (`meta.json`: смена роли без событий её поднимает).
Запись, которая не открылась (порча, чужой ключ), — разрыв: дальше её не
берётся, открытие идёт по тому, что до неё. Пачки основы — с номером после
`cb` снимка (раньше — учтены в нём).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import apply as _apply, counts, event
from .segments import Store


@dataclass
class Opened:
    epoch: int = 0
    role: str = "holder"
    fork: int = 0                                # последний номер события основы в базе гостя
    events: int = 0                              # события основы после снимка
    guest_events: int = 0
    counts: int = 0
    lost: list[str] = field(default_factory=list)
    gap: int = 0                                 # первый недостающий номер (события после него не взяты)
    gap_n: int = 0                               # сколько записей хранилища за разрывом не взято


def _events(store: Store, k: bytes, line: str, after: int) -> tuple[list[_apply.Event], int]:
    """События хранилища с номером больше after подряд; (события, первый недостающий или 0).
    Не открылась — тоже разрыв."""
    out, nxt = [], after + 1
    for raw in store.events(after=after):
        try:
            ev = event.open_event(k, raw, line)
        except event.RecordError:
            return out, nxt
        if ev.i != nxt:
            return out, nxt
        out.append(ev)
        nxt += 1
    return out, 0


def _meta_e(keep_dir: Path, line: str) -> int:
    """Эпоха линии, известная резерву (его отметка в хранилище); не та линия или нет — 0."""
    try:
        data = json.loads((keep_dir / "meta.json").read_text(encoding="utf-8"))
        return int(data.get("e") or 0) if str(data.get("line") or "") == line else 0
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def build(db_path: str | Path, k: bytes, marks: dict, keep_dir: str | Path, guest_dir: str | Path,
          *, button: bool) -> Opened:
    line = str(marks.get("line") or "")
    snap_i, snap_e, cb = int(marks.get("i") or 0), int(marks.get("e") or 0), int(marks.get("cb") or 0)
    res = Opened()
    keep, mine = Store(keep_dir), Store(guest_dir)
    base_ev, res.gap = _events(keep, k, line, snap_i)
    if res.gap:
        res.gap_n = max(1, keep.last()[1] - res.gap + 1)   # от разрыва до последнего у резерва: всё это не взято
    fork = base_ev[-1].i if base_ev else snap_i
    guest_ev, _gap = _events(mine, k, line, fork) if mine.events() else ([], 0)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("INSERT OR IGNORE INTO jrn_meta (id) VALUES (1)")
        con.execute("UPDATE jrn_meta SET applied = ?, applying = 0 WHERE id = 1", (snap_i,))
        for key, val in (("db_line", line), ("last_monthly_reset", str(marks.get("month") or ""))):
            if val:
                con.execute("INSERT INTO server_state (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, val))
        con.commit()
        done = _apply.apply(con, base_ev + guest_ev)
        res.lost = [f"{ev.i} {ev.op} {ev.t}: {why}" for ev, why in done.lost]
        res.events, res.guest_events, res.fork = len(base_ev), len(guest_ev), fork
        month = counts.current_month(con)
        con.execute("INSERT INTO jrn_seen (host, s) VALUES (?, ?) ON CONFLICT(host) DO UPDATE SET s = excluded.s",
                    (f"{line}:a", cb))                          # пачки основы по cb — в снимке
        con.commit()
        last_b = 0
        for side, store, after in (("a", keep, cb), ("b", mine, 0)):
            for raw in store.counts(side, after=after):
                try:
                    got_side, batch = event.open_counts(k, raw, line)
                except event.RecordError:
                    continue
                if got_side == side and counts.apply(con, batch, month):
                    res.counts += 1
                if side == "b":
                    last_b = max(last_b, batch.bn)
        known_e = _meta_e(Path(keep_dir), line)
        base_e = max([snap_e, known_e] + [ev.e for ev in base_ev])
        guest_e = max([ev.e for ev in guest_ev], default=0)
        if guest_e:
            res.epoch, res.role, holder = guest_e, "holder", "self"
        elif button:
            res.epoch, res.role, holder = base_e, "follower", "peer"
        else:
            res.epoch, res.role, holder = base_e + 1, "holder", "self"
        last_guest = guest_ev[-1].i if guest_ev else fork
        if last_b:                                             # счёт пачек гостя — после записанных им
            con.execute("UPDATE jrn_meta SET cnt_bn = ?, cnt_ack = ? WHERE id = 1", (last_b, last_b))
        con.execute("UPDATE jrn_meta SET epoch = ?, holder = ?, role = ?, capture = 1, applied = ?, acked = ?, "
                    "fork_i = ?, why = ? WHERE id = 1",
                    (res.epoch, holder, res.role, fork, fork, fork,
                     "открытие по журналу" + (" (журнал гостя)" if guest_ev else "")))
        if last_guest > fork:                                   # номера гостя не переиспользуются
            con.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'jrn_events' AND seq < ?",
                        (last_guest, last_guest))
        con.commit()
    finally:
        con.close()
    return res
