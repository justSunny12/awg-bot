"""
reserve.py — трёхстороннее слияние при возврате пользователей с резерва.

Три базы: `base` — снимок, с которого резерв начал (`reserve/out/base-<gen>.db`
у основы), `cur` — живая база основы (всё, что основа записала после снимка:
последние секунды перед смертью, трафик оставшихся сессий при переезде
кнопкой), `rep` — реплика бота основы на резерве на момент передачи.
Итог строится **на базе основы**: всё про хост (слоты, пара, базы счётчиков
ядра) остаётся её, а в неё переносится всё, что бот основы на резерве изменил
относительно снимка — произошедшее на резерве позже по времени и потому
главнее. Правила по колонкам — `policy.py`.

Строки: есть в реплике, нет в снимке — создана на резерве: вставить (id занят
строкой, созданной основой после снимка, — новый id с переносом ссылок);
есть в снимке, нет в реплике — удалена на резерве: удалить с каскадом; есть в
обеих — поля по правилам. Конфликт уникальности (адрес, ключ устройства,
tg_id, инвайт) между строкой резерва и строкой, созданной основой после
снимка, — строка резерва остаётся (ею пользовались), строка основы — в отчёт
`lost`. Счётчик на резерве меньше снимка (сброс месяца на резерве) — значение
реплики, а дельта основы за прошлый месяц — в `traffic_monthly`.

Вход — пути трёх файлов SQLite, выход — итог в `cur` (на месте, под
остановленным ботом) и отчёт. Вызывающий (возврат, этап 4) затем сверяет пиры
с конфигом, блокировки и делает `recheck`.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from awgbot.util import timeutil
from awgbot.util.rsvpkg.bot import GUEST_STATE

from . import policy as P
from .data import _cols


@dataclass
class Report:
    inserted: dict[str, int] = field(default_factory=dict)      # таблица → вставлено строк
    deleted: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    remapped: dict[str, int] = field(default_factory=dict)      # строк с новым id
    lost: list[dict] = field(default_factory=list)              # строки основы, не вставшие из-за конфликта
    month_archived: int = 0                                     # устройств с архивом прошлого месяца
    appended: dict[str, int] = field(default_factory=dict)


class MergeError(RuntimeError):
    pass


def _row_key(row: sqlite3.Row, key: tuple[str, ...]) -> tuple:
    return tuple(row[k] for k in key)


def _rows(con: sqlite3.Connection, schema: str, table: str, cols: list[str], key: tuple[str, ...]) -> dict[tuple, sqlite3.Row]:
    try:
        rows = con.execute(f"SELECT {','.join(cols)} FROM {schema}.{table}").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {_row_key(r, key): r for r in rows}


def _merge_value(rule: str, base, cur, rep):
    if rule == P.CHANGED:
        return rep if rep != base else cur
    if rule == P.MASK:
        b, c, r = int(base or 0), int(cur or 0), int(rep or 0)
        set_bits, cleared = r & ~b, b & ~r
        return (c | set_bits) & ~cleared
    if rule == P.MAX:
        return max(int(cur or 0), int(rep or 0)) or None if (cur is None and rep is None) else max(int(cur or 0), int(rep or 0))
    if rule == P.CUR or rule == P.KEY:
        return cur
    if rule == P.REP:
        return rep
    raise MergeError(f"неизвестное правило {rule!r}")


def _counter(base, cur, rep) -> tuple[int, int | None]:
    """(итог, дельта основы в архив прошлого месяца или None)."""
    b, c, r = int(base or 0), int(cur or 0), int(rep or 0)
    if r < b:                                  # сброс месяца на резерве
        return r, max(0, c - b)
    return c + (r - b), None


def merge(cur_db: str, base_db: str, rep_db: str, *, admin_tg: int = 0, no_delete: bool = False) -> Report:
    """Слить реплику в живую базу основы на месте. Бот основы — остановлен.
    `no_delete` — база не настоящий снимок (его нет: основа восстановлена из
    копии), удалённое на резерве не выводится, строки только добавляются и
    обновляются."""
    rpt = Report()
    con = sqlite3.connect(cur_db)
    con.row_factory = sqlite3.Row
    try:
        con.execute("ATTACH DATABASE ? AS base", (base_db,))
        con.execute("ATTACH DATABASE ? AS rep", (rep_db,))
        con.execute("PRAGMA foreign_keys = OFF")
        _check_schema(con)
        con.execute("BEGIN")
        try:
            id_map: dict[str, dict[int, int]] = {"clients": {}, "devices": {}}
            for table in ("clients", "devices"):
                _merge_table(con, table, rpt, id_map, no_delete)
            for table in P.TABLES:
                if table not in ("clients", "devices"):
                    _merge_table(con, table, rpt, id_map, no_delete)
            _merge_state(con, rpt)
            for table in P.TABLE_REP:
                _replace_table(con, table, rpt)
            for table in P.TABLE_APPEND:
                _append_table(con, table, rpt, id_map)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    return rpt


def _check_schema(con) -> None:
    """У таблиц данных пользователей в живой базе нет колонок без правила."""
    for table, rules in P.TABLES.items():
        have = set(_cols(con, table))
        missing = have - set(P.columns(table))
        if missing:
            raise MergeError(f"в {table} колонки без правила слияния: {sorted(missing)}")


def _common_cols(con, table: str, schema_a: str, schema_b: str) -> list[str]:
    a, b = set(_cols(con, table, schema_a)), set(_cols(con, table, schema_b))
    return [c for c in _cols(con, table, "main") if c in a and c in b]


def _merge_table(con, table: str, rpt: Report, id_map: dict, no_delete: bool = False) -> None:
    rules = P.columns(table)
    key = P.key_of(table)
    cols = [c for c in _common_cols(con, table, "base", "rep") if c in rules]
    if not cols:
        return
    base_rows = _rows(con, "base", table, cols, key)
    rep_rows = _rows(con, "rep", table, cols, key)
    cur_rows = _rows(con, "main", table, cols, key)
    parent = P.TABLES[table].get("_parent")
    # сначала удалённое на резерве: освобождённые им уникальные значения
    # (адрес, tg_id) могли занять новые строки резерва — они вставляются ниже
    for k in base_rows:
        if no_delete:
            break                                         # база — не снимок: «удалено на резерве» не выводится
        if k not in rep_rows and k in cur_rows:
            _delete_row(con, table, key, k, rpt)
            cur_rows.pop(k, None)
    for k, rrow in rep_rows.items():
        rk = _remap_key(table, k, parent, id_map)
        if k in base_rows:
            brow = base_rows[k]
            crow = cur_rows.get(rk)
            if crow is None:
                continue                                  # основа удалила строку после снимка — её решение
            _update_row(con, table, rules, cols, key, rk, brow, crow, rrow, rpt, id_map)
        elif rk in cur_rows and table not in ("clients", "devices"):
            # ленивая строка 1:1 (отсрочка, пауза), заведённая с обеих сторон после
            # снимка: снимка нет — значение резерва главнее, счётчики складываются
            # (у таблиц со своим id совпадение ключа — две разные строки, см. _insert_row)
            _update_row(con, table, rules, cols, key, rk, None, cur_rows[rk], rrow, rpt, id_map)
        else:
            _insert_row(con, table, rules, cols, key, rk, rrow, rpt, id_map, parent)


def _remap_key(table: str, k: tuple, parent, id_map: dict) -> tuple:
    """Ключ строки реплики в координатах основы: родительский id мог переехать."""
    if parent and parent[0] in id_map:
        pk = P.key_of(table)
        out = list(k)
        for i, name in enumerate(pk):
            if name == parent[1] and k[i] in id_map[parent[0]]:
                out[i] = id_map[parent[0]][k[i]]
        return tuple(out)
    return k


def _ref_value(table: str, col: str, value, id_map: dict):
    """Значение ссылочной колонки реплики — через карту id."""
    for parent, refs in P.REFS.items():
        for t, c in refs:
            if t == table and c == col and value is not None and int(value) in id_map.get(parent, {}):
                return id_map[parent][int(value)]
    return value


def _update_row(con, table, rules, cols, key, rk, brow, crow, rrow, rpt, id_map) -> None:
    sets, vals, archive = [], [], {}
    for c in cols:
        rule = rules[c]
        if rule == P.KEY:
            continue
        b = brow[c] if brow is not None else None
        if rule == P.COUNTER:
            v, delta = _counter(b, crow[c], rrow[c])
            if delta is not None:
                archive[c] = delta
        else:
            r = _ref_value(table, c, rrow[c], id_map)
            v = _merge_value(rule, b, crow[c], r)
        if v != crow[c]:
            sets.append(f"{c} = ?")
            vals.append(v)
    if archive and table == "device_traffic":
        _archive_prev_month(con, int(rk[0]), archive, rpt)
    if sets:
        where = " AND ".join(f"{k} = ?" for k in key)
        con.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE {where}", vals + list(rk))
        rpt.updated[table] = rpt.updated.get(table, 0) + 1


def _insert_row(con, table, rules, cols, key, rk, rrow, rpt, id_map, parent) -> None:
    values = {c: _ref_value(table, c, rrow[c], id_map) for c in cols}
    for i, name in enumerate(key):
        values[name] = rk[i]
    if table in ("clients", "devices"):
        _resolve_unique(con, table, values, rpt)
        old_id = int(values["id"])
        taken = con.execute(f"SELECT 1 FROM {table} WHERE id = ?", (old_id,)).fetchone()
        if taken is not None:
            values.pop("id")                              # id занят строкой основы после снимка — новый
    names = list(values)
    con.execute(f"INSERT OR IGNORE INTO {table} ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                [values[n] for n in names])
    if table in ("clients", "devices"):
        new_id = int(con.execute("SELECT last_insert_rowid()").fetchone()[0]) if "id" not in values else int(values["id"])
        if "id" not in values:
            id_map[table][int(rrow["id"])] = new_id
            rpt.remapped[table] = rpt.remapped.get(table, 0) + 1
    rpt.inserted[table] = rpt.inserted.get(table, 0) + 1


def _resolve_unique(con, table: str, values: dict, rpt: Report) -> None:
    """Строка основы, созданная после снимка и занявшая уникальное значение
    строки резерва, уступает: в отчёт и прочь (с каскадом)."""
    for col in P.UNIQUE.get(table, ()):
        v = values.get(col)
        if v is None:
            continue
        row = con.execute(f"SELECT id, name FROM {table} WHERE {col} = ?", (v,)).fetchone()
        if row is None:
            continue
        in_base = con.execute(f"SELECT 1 FROM base.{table} WHERE id = ?", (row["id"],)).fetchone()
        if in_base is not None:
            continue                                      # строка была в снимке — реплика про неё знает (обновление)
        rpt.lost.append({"table": table, "id": int(row["id"]), "name": row["name"], "column": col, "value": v})
        _delete_row(con, table, ("id",), (int(row["id"]),), rpt, count=False)


def _delete_row(con, table: str, key: tuple[str, ...], k: tuple, rpt: Report, count: bool = True) -> None:
    where = " AND ".join(f"{c} = ?" for c in key)
    cascade = P.TABLES.get(table, {}).get("_cascade")
    if cascade == "device":
        for t, c in P.REFS["devices"]:
            if t == "devices":
                con.execute("UPDATE devices SET twin_of = NULL WHERE twin_of = ?", (k[0],))
            else:
                con.execute(f"DELETE FROM {t} WHERE {c} = ?", (k[0],))
    elif cascade == "client":
        for d in con.execute("SELECT id FROM devices WHERE client_id = ?", (k[0],)).fetchall():
            _delete_row(con, "devices", ("id",), (int(d[0]),), rpt, count=False)
        for t, c in P.REFS["clients"]:
            if t != "devices":
                con.execute(f"DELETE FROM {t} WHERE {c} = ?", (k[0],))
        con.execute("UPDATE devices SET holder_client_id = NULL WHERE holder_client_id = ?", (k[0],))
    con.execute(f"DELETE FROM {table} WHERE {where}", list(k))
    if count:
        rpt.deleted[table] = rpt.deleted.get(table, 0) + 1


def _archive_prev_month(con, device_id: int, deltas: dict, rpt: Report) -> None:
    """Сброс месяца случился на резерве: дельты основы за прошлый месяц — в архив."""
    if not any(deltas.values()):
        return
    prev = timeutil.shift(timeutil.now().replace(day=1), -1)
    month = prev.strftime("%Y-%m")
    client = con.execute("SELECT client_id FROM devices WHERE id = ?", (device_id,)).fetchone()
    vals = {P.MONTH_ARCHIVE[c]: int(v) for c, v in deltas.items() if c in P.MONTH_ARCHIVE}
    row = con.execute("SELECT id, rx, tx, rf_rx, rf_tx FROM traffic_monthly WHERE device_id = ? AND month = ?",
                      (device_id, month)).fetchone()
    if row is not None:
        con.execute("UPDATE traffic_monthly SET rx = rx + ?, tx = tx + ?, rf_rx = rf_rx + ?, rf_tx = rf_tx + ? WHERE id = ?",
                    (vals.get("rx", 0), vals.get("tx", 0), vals.get("rf_rx", 0), vals.get("rf_tx", 0), row["id"]))
    else:
        con.execute("INSERT INTO traffic_monthly (device_id, client_id, month, rx, tx, rf_rx, rf_tx, archived_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (device_id, client[0] if client else None, month, vals.get("rx", 0), vals.get("tx", 0),
                     vals.get("rf_rx", 0), vals.get("rf_tx", 0), timeutil.now_iso()))
    rpt.month_archived += 1


def _merge_state(con, rpt: Report) -> None:
    """`server_state`: только ключи белого списка пакета — значение реплики, если
    оно отличается от снимка."""
    for key in GUEST_STATE:
        b = con.execute("SELECT value FROM base.server_state WHERE key = ?", (key,)).fetchone()
        r = con.execute("SELECT value FROM rep.server_state WHERE key = ?", (key,)).fetchone()
        bv, rv = (b[0] if b else None), (r[0] if r else None)
        if rv == bv:
            continue
        if rv is None:
            con.execute("DELETE FROM server_state WHERE key = ?", (key,))
        else:
            con.execute("INSERT INTO server_state (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, rv))
        rpt.updated["server_state"] = rpt.updated.get("server_state", 0) + 1


def _replace_table(con, table: str, rpt: Report) -> None:
    cols = _common_cols(con, table, "rep", "main")
    if not cols:
        return
    con.execute(f"DELETE FROM {table}")
    con.execute(f"INSERT INTO {table} ({','.join(cols)}) SELECT {','.join(cols)} FROM rep.{table}")
    rpt.updated[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _append_table(con, table: str, rpt: Report, id_map: dict) -> None:
    """История: строки реплики дописываются (в снимке истории не было — всё
    новое); ссылки на переехавшие id переводятся."""
    cols = [c for c in _common_cols(con, table, "rep", "main") if c != "id"]
    if not cols:
        return
    rows = con.execute(f"SELECT {','.join(cols)} FROM rep.{table}").fetchall()
    # архив месяца обе стороны могли закрыть сами (граница месяца в away): строка за тот же
    # месяц прибавляется к существующей, а не дописывается второй
    monthly_key = {"traffic_monthly": ("device_id", "month"), "server_traffic_monthly": ("month",)}.get(table)
    counters = ("rx", "tx", "rf_rx", "rf_tx")
    n = 0
    for r in rows:
        vals = []
        for c in cols:
            v = r[c]
            if c in ("client_id",) and v is not None and int(v) in id_map.get("clients", {}):
                v = id_map["clients"][int(v)]
            if c in ("device_id",) and v is not None and int(v) in id_map.get("devices", {}):
                v = id_map["devices"][int(v)]
            vals.append(v)
        if monthly_key and all(k in cols for k in monthly_key):
            row = dict(zip(cols, vals))
            where = " AND ".join(f"{k} = ?" for k in monthly_key)
            hit = con.execute(f"SELECT id FROM {table} WHERE {where}", [row[k] for k in monthly_key]).fetchone()
            if hit is not None:
                add = [c for c in counters if c in cols]
                if add:
                    con.execute(f"UPDATE {table} SET " + ", ".join(f"{c} = {c} + ?" for c in add) + " WHERE id = ?",
                                [int(row.get(c) or 0) for c in add] + [hit[0]])
                n += 1
                continue
        con.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        n += 1
    if n:
        rpt.appended[table] = n
