"""
rebase.py — часть пакета rsvjrn (см. __init__): перебазирование хвоста старой
эпохи поверх базы держателя.

Хвост — события бывшего держателя после точки расхождения (`jrn_tail`). Журнала
новой эпохи для сравнения нет (подтверждённые события удаляются), поэтому
сравнение — по значениям: у события есть прежние значения колонок (`o`) — то,
что было на этом месте до правки хвоста. Для каждой изменённой колонки:

| в базе держателя сейчас | итог |
|---|---|
| прежнее значение хвоста | правка хвоста применяется |
| новое значение хвоста | уже так — ничего |
| другое | **спорное**: остаётся значение держателя |

- маска блокировок — побитно: бит, поставленный или снятый хвостом, ставится,
  если держатель этот бит не менял (менял так же — тоже не спор);
- вставка — как есть; `id` занят — новый `id` и перенос ссылок в следующих
  событиях хвоста; конфликт уникальности (адрес, ключ, `tg_id`, приглашение)
  — спорное;
- удаление — если строка у держателя та же, что была; иначе спорное;
- правка строки, которой у держателя уже нет, — спорное;
- `month`, `period` — пропуск: сбросы по времени держатель делает сам;
- `tmerge` — обе строки на месте — сложить; иначе пропуск.

Применённое пишется обычным соединением держателя — захват превращает его в
события его эпохи, и они идут второй стороне обычным порядком. Отчёт — полный
список спорного (`Report.disputed`), без обрезки: строки и снимок списка
делает основа.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .apply import Event, _columns, _counter_op, _drop_null_secrets, _q
from .classes import MASK_COLUMNS, NOT_TAIL, SECRET_COLUMNS

# уникальные значения, за которые спорят строки хвоста и держателя
UNIQUE: dict[str, tuple[str, ...]] = {"devices": ("address", "public_key"), "clients": ("tg_id", "invite_code")}
# ссылки на id: при вставке под новым id — перенос в следующих событиях хвоста
REFS: dict[str, list[tuple[str, str]]] = {
    "clients": [("devices", "client_id"), ("devices", "holder_client_id"), ("client_subscription", "client_id"),
                ("client_quota", "client_id"), ("client_grace", "client_id"), ("client_pause", "client_id"),
                ("client_routing_domains", "client_id")],
    "devices": [("device_traffic", "device_id"), ("device_friend", "device_id"), ("migration_cohort", "device_id"),
                ("devices", "twin_of")],
}
REMAPPED = ("clients", "devices")
# служебные колонки: спор по ним — не спор (держатель прав, админу это шум)
QUIET: dict[str, tuple[str, ...]] = {
    "clients": ("tg_name", "tg_name_at", "tg_username"), "client_subscription": ("notified_thresholds",),
    "client_quota": ("traffic_notified", "bonus_granted_month"), "client_grace": ("grace_pending_cut",),
}
# значения, которые в отчёт (и в Т-Ж4, и в журнал) не идут — не путать с SECRET_COLUMNS реестра
# (пустой секрет основы — «не трогать»): здесь только то, что нельзя показывать
HIDDEN_VALUE_COLS: dict[str, tuple[str, ...]] = {
    "devices": ("private_key", "preshared_key"), "clients": ("invite_code",), "client_pause": ("resume_code",),
    "device_friend": ("friend_code",), "server_state": ("value",),
}


@dataclass
class Report:
    moved: int = 0
    skipped: int = 0                                          # не хвост: данные хоста, сбросы по времени, таблицы нет
    disputed: list[dict] = field(default_factory=list)


def _where(k: dict) -> tuple[str, list]:
    return " AND ".join(f"{_q(c)} IS ?" for c in k), list(k.values())


def _remap(table: str, values: dict, id_map: dict) -> dict:
    out = dict(values)
    history = table.endswith("_histories")                    # история — ссылки по имени колонки
    for parent, refs in REFS.items():
        cols = [c for t, c in refs if t == table]
        if history:
            cols += ["client_id"] if parent == "clients" else ["device_id"]
        for c in cols:
            if _int(out.get(c)) is not None:
                out[c] = id_map[parent].get(_int(out[c]), out[c])
    if table in REMAPPED and _int(out.get("id")) is not None:
        out["id"] = id_map[table].get(_int(out["id"]), out["id"])
    return out


def _row(conn, table: str, k: dict):
    where, args = _where(k)
    prev, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        return conn.execute(f"SELECT * FROM main.{_q(table)} WHERE {where}", args).fetchone()
    finally:
        conn.row_factory = prev                              # соединение держателя — дальше всему процессу


def _client_of(conn, table: str, k: dict, vals: dict) -> int | None:
    """Профиль, к которому относится строка (для строки отчёта)."""
    if table == "clients":
        return _int(k.get("id"))
    if vals.get("client_id") is not None:
        return _int(vals["client_id"])
    if k.get("client_id") is not None:
        return _int(k["client_id"])
    dev = vals.get("device_id", k.get("device_id")) if table != "devices" else k.get("id")
    if dev is not None:
        row = conn.execute("SELECT client_id FROM main.devices WHERE id = ?", (dev,)).fetchone()
        return _int(row[0]) if row else None
    return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _taken_by(conn, table: str, vals: dict) -> dict | None:
    """Чья строка держателя заняла уникальное значение вставки хвоста."""
    for col in UNIQUE.get(table, ()):
        v = vals.get(col)
        if v is None:
            continue
        row = _row(conn, table, {col: v})
        if row is not None:
            keys = row.keys()
            return {"col": col, "id": row["id"], "name": row["name"] if "name" in keys else "",
                    "client_id": row["client_id"] if "client_id" in keys else row["id"]}
    return None


def _mask(base: int, mine: int, cur: int) -> tuple[int, bool]:
    """Побитно: биты, которые хвост менял (base → mine), — его, если держатель их не менял
    по-другому; (итог, был ли спор)."""
    base, mine, cur = int(base or 0), int(mine or 0), int(cur or 0)
    tail_bits = base ^ mine
    holder_bits = base ^ cur
    clash = tail_bits & holder_bits & (mine ^ cur)
    result = (cur & ~tail_bits) | (mine & tail_bits)
    result = (result & ~clash) | (cur & clash)
    return result, bool(clash)


def rebase(conn: sqlite3.Connection, events: list[Event], *, before_commit=None) -> Report:
    """Перенести хвост поверх базы держателя одной транзакцией (каждое событие — своей
    точкой сохранения: спорное и не вставшее не отменяют остального). before_commit(conn,
    report) — в той же транзакции (отметка «хвост перенесён»)."""
    rpt = Report()
    id_map: dict[str, dict[int, int]] = {t: {} for t in REMAPPED}
    failed: dict[str, set] = {t: set() for t in REMAPPED}     # вставки, не вставшие по уникальности
    tables = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for ev in sorted(events, key=lambda x: x.i):
            if _orphan(ev, failed):
                # строка хвоста не встала — её дальнейшие правки не должны лечь на чужую строку держателя
                rpt.disputed.append({"kind": "gone", "table": ev.t, "key": dict(ev.k), "op": ev.op,
                                     "client_id": None, "col": "", "tail": None, "kept": None, "taken_by": None,
                                     "i": ev.i, "name": str((ev.c or {}).get("name") or (ev.o or {}).get("name") or ""),
                                     "orphan": True})
                continue
            conn.execute("SAVEPOINT jrn_rb")
            try:
                n = _one(conn, ev, id_map, tables, rpt)
                conn.execute("RELEASE jrn_rb")
            except sqlite3.IntegrityError as e:
                conn.execute("ROLLBACK TO jrn_rb")
                conn.execute("RELEASE jrn_rb")
                vals = _remap(ev.t, dict(ev.c or {}), id_map)
                tb = _taken_by(conn, ev.t, vals) if ev.op in ("I", "U") else None
                kind = "unique" if "UNIQUE" in str(e).upper() else "failed"
                if ev.op == "I" and ev.t in REMAPPED and ev.k.get("id") is not None:
                    failed[ev.t].add(int(ev.k["id"]))
                rpt.disputed.append({"kind": kind, "table": ev.t, "key": dict(ev.k), "op": ev.op,
                                     "client_id": _client_of(conn, ev.t, ev.k, vals), "col": tb["col"] if tb else "",
                                     "tail": {k: vals.get(k) for k in ("name", "address", "tg_id")
                                              if vals.get(k) is not None}, "kept": None,
                                     "taken_by": tb, "why": str(e)[:160], "i": ev.i})
                continue
            if n:
                rpt.moved += 1
        for d in rpt.disputed:
            if d.get("col") in HIDDEN_VALUE_COLS.get(str(d.get("table")), ()):
                d.update(tail=None, kept=None, secret=True)    # значения ключей и кодов — никуда
        if before_commit is not None:
            before_commit(conn, rpt)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return rpt


def _orphan(ev: Event, failed: dict) -> bool:
    """Событие про строку (или её детей), чья вставка из хвоста не встала."""
    vals = {**(ev.o or {}), **(ev.c or {}), **(ev.k or {})}
    for parent, ids in failed.items():
        if not ids:
            continue
        if ev.t == parent and _int(vals.get("id")) in ids:
            return True
        cols = [c for t, c in REFS[parent] if t == ev.t] + (["client_id"] if parent == "clients" else ["device_id"])
        if any(_int(vals.get(c)) in ids for c in cols if vals.get(c) is not None):
            return True
    return False


def _one(conn, ev: Event, id_map: dict, tables: set, rpt: Report) -> bool:
    """Одно событие хвоста; True — что-то применено."""
    if ev.t in NOT_TAIL:
        rpt.skipped += 1
        return False
    if ev.op in ("month", "period"):
        rpt.skipped += 1                                     # сбросы по времени держатель делает сам
        return False
    if ev.op == "tmerge":
        src = id_map["devices"].get(int(ev.k["src"]), int(ev.k["src"]))
        dst = id_map["devices"].get(int(ev.k["dst"]), int(ev.k["dst"]))
        have = {r[0] for r in conn.execute("SELECT device_id FROM main.device_traffic WHERE device_id IN (?, ?)",
                                            (src, dst))}
        if have != {src, dst}:
            rpt.skipped += 1
            return False
        _counter_op(conn, Event(i=ev.i, e=ev.e, t=ev.t, op="tmerge", k={"src": src, "dst": dst}))
        return True
    if ev.t not in tables:
        rpt.skipped += 1
        return False
    k = _remap(ev.t, dict(ev.k), id_map)
    if ev.op == "I":
        return _insert(conn, ev, k, id_map, rpt)
    if ev.op == "U":
        return _update(conn, ev, k, id_map, rpt)
    if ev.op == "D":
        return _delete(conn, ev, k, rpt)
    rpt.skipped += 1
    return False


def _insert(conn, ev: Event, k: dict, id_map: dict, rpt: Report) -> bool:
    cols = set(_columns(conn, ev.t))
    vals = _drop_null_secrets(ev.t, {c: v for c, v in _remap(ev.t, {**(ev.c or {}), **k}, id_map).items() if c in cols})
    if ev.t in REMAPPED and vals.get("id") is not None and \
            conn.execute(f"SELECT 1 FROM main.{_q(ev.t)} WHERE id = ?", (vals["id"],)).fetchone():
        old = int(ev.k.get("id"))
        vals.pop("id")                                       # id занят строкой держателя — новый
        _ins(conn, ev.t, vals)
        id_map[ev.t][old] = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        return True
    if ev.t not in REMAPPED and k and _row(conn, ev.t, k) is not None:
        # строка с этим ключом у держателя уже есть (профиль один, подписка одна) — как правка
        return _update(conn, Event(i=ev.i, e=ev.e, t=ev.t, op="U", k=ev.k, c=ev.c, o={}), k, id_map, rpt)
    _ins(conn, ev.t, vals)
    return True


def _ins(conn, table: str, vals: dict) -> None:
    names = list(vals)
    conn.execute(f"INSERT INTO main.{_q(table)} ({', '.join(_q(c) for c in names)}) "
                 f"VALUES ({', '.join('?' for _ in names)})", [vals[c] for c in names])


def _update(conn, ev: Event, k: dict, id_map: dict, rpt: Report) -> bool:
    row = _row(conn, ev.t, k)
    # секрет основы у бота на резерве пустой: не сравнивать; пустое — «не трогать» (apply)
    c = _drop_null_secrets(ev.t, _remap(ev.t, dict(ev.c or {}), id_map))
    o = _drop_null_secrets(ev.t, _remap(ev.t, dict(ev.o or {}), id_map))
    if row is None:
        rpt.disputed.append({"kind": "gone", "table": ev.t, "key": dict(ev.k), "op": "U",
                             "client_id": _client_of(conn, ev.t, k, c), "col": "", "tail": None, "kept": None,
                             "taken_by": None, "i": ev.i, "name": str(o.get("name") or c.get("name") or "")})
        return False
    have = set(row.keys())
    sets: dict = {}
    masks = MASK_COLUMNS.get(ev.t, ())
    for col, mine in c.items():
        if col in k or col not in have:
            continue
        if o and col in o and o[col] == mine:
            continue                                         # хвост эту колонку не менял
        cur = row[col]
        if cur == mine:
            continue
        if col in QUIET.get(ev.t, ()):
            if o and col in o and cur == o[col]:
                sets[col] = mine                              # служебное — применить, если держатель не трогал
            continue                                          # иначе — его значение, без спора
        base = o.get(col, cur) if o else None
        if col in masks and base is not None:
            res, clash = _mask(base, mine, cur)
            if res != cur:
                sets[col] = res
            if clash:
                rpt.disputed.append(_dispute(conn, ev, k, c, col, mine, cur))
            continue
        if o and col in o and cur == base:
            sets[col] = mine
        else:
            rpt.disputed.append(_dispute(conn, ev, k, c, col, mine, cur))
    if not sets:
        return False
    where, args = _where(k)
    conn.execute(f"UPDATE main.{_q(ev.t)} SET {', '.join(f'{_q(x)} = ?' for x in sets)} WHERE {where}",
                 list(sets.values()) + args)
    return True


def _delete(conn, ev: Event, k: dict, rpt: Report) -> bool:
    row = _row(conn, ev.t, k)
    if row is None:
        return False
    secret = SECRET_COLUMNS.get(ev.t, ())
    o = {x: v for x, v in dict(ev.o or {}).items() if x not in secret}
    changed = [col for col, v in o.items() if col in row.keys() and row[col] != v and col not in k]
    if changed:
        rpt.disputed.append({"kind": "delete", "table": ev.t, "key": dict(ev.k), "op": "D",
                             "client_id": _client_of(conn, ev.t, k, dict(row)), "col": changed[0],
                             "tail": None, "kept": row[changed[0]], "taken_by": None, "i": ev.i,
                             "name": row["name"] if "name" in row.keys() else ""})
        return False
    where, args = _where(k)
    conn.execute(f"DELETE FROM main.{_q(ev.t)} WHERE {where}", args)
    return True


def _dispute(conn, ev: Event, k: dict, vals: dict, col: str, tail, kept) -> dict:
    name = ""
    if ev.t in ("clients", "devices"):
        row = _row(conn, ev.t, k)
        name = row["name"] if row is not None and "name" in row.keys() else ""
    return {"kind": "column", "table": ev.t, "key": dict(ev.k), "op": ev.op, "client_id": _client_of(conn, ev.t, k, vals),
            "col": col, "tail": tail, "kept": kept, "taken_by": None, "i": ev.i, "name": name}
