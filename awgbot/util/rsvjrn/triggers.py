"""
triggers.py — часть пакета rsvjrn (см. __init__): временные триггеры
соединения — запрет записи решений у следующего и захват событий у держателя.

Ставятся на каждое соединение (`CREATE TEMP TRIGGER … ON main.<таблица>`):
живут, пока живо соединение, в файле базы их нет. Запрет — когда
`jrn_meta.role` не `holder` и это не транзакция применения (`applying = 0`;
применитель ставит 1 и снимает в той же транзакции — другие соединения отметки
не видят). Захват — когда `capture = 1` (пара с журналом) и не применение:
событие ложится в `jrn_events` той же транзакцией, что и запись. `UPDATE OF
<колонки решений>` не срабатывает на запись счётчиков опросом, условие
`IS NOT` — на запись того же значения. Сырые соединения (реплика, бэкап,
восстановление, слияние при возврате) триггеров не имеют.

Что захватывается, решает реестр классов (classes): решения и маски — всегда,
секреты и история — только у бота основы на резерве (guest): от основы к
резерву они не едут.
"""
from __future__ import annotations

import sqlite3

from . import REFUSAL_PREFIX
from .classes import STATE_DECISION, TABLES, captured_columns, denied_columns, history_captured

_MSG = f"{REFUSAL_PREFIX} эпоха не у этого процесса — решения пишет держатель"
_GUARD = ("(SELECT role FROM main.jrn_meta WHERE id = 1) <> 'holder' "
          "AND (SELECT applying FROM main.jrn_meta WHERE id = 1) = 0")
_CAPTURE = "FROM main.jrn_meta m WHERE m.id = 1 AND m.capture = 1 AND m.applying = 0"
_AT = "strftime('%Y-%m-%dT%H:%M:%f', 'now', 'localtime')"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _deny() -> str:
    return f"SELECT RAISE(ABORT, {_lit(_MSG)}) WHERE {_GUARD};"


def _obj(cols, side: str) -> str:
    """json_object('a', NEW."a", …); пусто — '{}'."""
    if not cols:
        return "'{}'"
    return "json_object(" + ", ".join(f"{_lit(c)}, {side}.{_q(c)}" for c in cols) + ")"


def _event(table: str, op: str, keys, side_k: str, c_cols, c_side: str | None, o_cols, o_side: str | None) -> str:
    c = _obj(c_cols, c_side) if c_side else "NULL"
    o = _obj(o_cols, o_side) if o_side else "NULL"
    return (f"INSERT INTO main.jrn_events (e, t, op, k, c, o, at) SELECT m.epoch, {_lit(table)}, {_lit(op)}, "
            f"{_obj(keys, side_k)}, {c}, {o}, {_AT} {_CAPTURE};")


def statements(schema: dict[str, list[tuple[str, int]]], guest: bool = False) -> list[str]:
    """CREATE TEMP TRIGGER для таблиц схемы: таблица → [(колонка, позиция в ключе
    или 0)] по порядку схемы. guest — бот основы на резерве (захват секретов и истории)."""
    out: list[str] = []
    if "jrn_meta" not in schema:
        return out                                    # схема старше эпохи — запрещать и захватывать не по чему
    has_events = "jrn_events" in schema
    for table, spec in schema.items():
        if table not in TABLES or table == "server_state":
            continue
        cols = [c for c, _pk in spec]
        keys = [c for c, pk in sorted(spec, key=lambda x: x[1]) if pk]
        deny = denied_columns(table, cols)
        cap = captured_columns(table, cols, guest) if has_events else []
        t, name = _q(table), table
        if history_captured(table, guest) and has_events:
            # история: дописка вместе с решением на резерве — основе (чистка по сроку — своя у каждой)
            body = _event(table, "I", [], "NEW", [c for c in cols if c not in keys], "NEW", [], None)
            out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_{name}_i AFTER INSERT ON main.{t} BEGIN {body} END")
            continue
        if not deny and not cap:
            continue
        whole = bool(deny) and len(deny) == len(cols)
        if whole or cap:
            body = (_deny() if whole else "") + (_event(table, "I", keys, "NEW", cap, "NEW", [], None) if cap else "")
            out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_{name}_i AFTER INSERT ON main.{t} BEGIN {body} END")
        if whole or cap:
            body = (_deny() if whole else "") + (_event(table, "D", keys, "OLD", [], None, cap, "OLD") if cap else "")
            out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_{name}_d AFTER DELETE ON main.{t} BEGIN {body} END")
        watch = list(dict.fromkeys(deny + cap))
        if watch:
            changed = " OR ".join(f"OLD.{_q(c)} IS NOT NEW.{_q(c)}" for c in watch)
            of = ", ".join(_q(c) for c in watch)
            body = (_deny() if deny else "") + (_event(table, "U", keys, "OLD", cap, "NEW", cap, "OLD") if cap else "")
            out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_{name}_u AFTER UPDATE OF {of} ON main.{t} "
                       f"WHEN {changed} BEGIN {body} END")
    if "server_state" in schema and STATE_DECISION:
        keys = ", ".join(_lit(k) for k in STATE_DECISION)
        ev = has_events
        ins = _event("server_state", "I", ["key"], "NEW", ["value"], "NEW", [], None) if ev else ""
        upd = _event("server_state", "U", ["key"], "NEW", ["value"], "NEW", ["value"], "OLD") if ev else ""
        dele = _event("server_state", "D", ["key"], "OLD", [], None, ["value"], "OLD") if ev else ""
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_state_i AFTER INSERT ON main.server_state "
                   f"WHEN NEW.key IN ({keys}) BEGIN {_deny()} {ins} END")
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_state_u AFTER UPDATE ON main.server_state "
                   f"WHEN (NEW.key IN ({keys}) OR OLD.key IN ({keys})) AND (OLD.value IS NOT NEW.value "
                   f"OR OLD.key IS NOT NEW.key) BEGIN {_deny()} {upd} END")
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_state_d AFTER DELETE ON main.server_state "
                   f"WHEN OLD.key IN ({keys}) BEGIN {_deny()} {dele} END")
    return out


def schema_of(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """Таблица → [(колонка, позиция в первичном ключе или 0)] по порядку схемы."""
    tables = [r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")]
    return {t: [(r[1], int(r[5])) for r in conn.execute(f"PRAGMA main.table_info({_q(t)})")] for t in tables}


def install(conn: sqlite3.Connection, guest: bool = False) -> bool:
    """Поставить триггеры на соединение; False — схемы ещё нет (первый старт до
    init_schema): поставит следующий вызов после создания таблиц."""
    stmts = statements(schema_of(conn), guest)
    if not stmts:
        return False
    for s in stmts:
        conn.execute(s)
    return True
