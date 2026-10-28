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

Запрет и захват — **разные триггеры**: захват требует JSON-функций SQLite и
не встаёт без них (или на сборке, которая отвергнет его тело), а запрет от
этого не должен пропасть никогда. Таблица, в которую пишет тело триггера, —
без имени схемы: SQLite (3.46 Debian 13) не принимает `INSERT INTO main.…`
внутри триггера; во временном триггере имя без схемы находит таблицу main.

Что захватывается, решает реестр классов (classes): решения и маски — всегда,
секреты и история — только у бота основы на резерве (guest), хост основы —
только у основы.
"""
from __future__ import annotations

import logging
import sqlite3

from . import REFUSAL_PREFIX, json_ok
from .classes import STATE_DECISION, TABLES, captured_columns, denied_columns, history_captured

log = logging.getLogger("awgbot.db")

_MSG = f"{REFUSAL_PREFIX} эпоха не у этого процесса — решения пишет держатель"
_GUARD = ("(SELECT role FROM main.jrn_meta WHERE id = 1) <> 'holder' "
          "AND (SELECT applying FROM main.jrn_meta WHERE id = 1) = 0")
_CAPTURE = "FROM main.jrn_meta m WHERE m.id = 1 AND m.capture = 1 AND m.applying = 0"
_AT = "strftime('%Y-%m-%dT%H:%M:%f', 'now') || '+00:00'"   # с поясом: наивное местное время хоста читалось бы как UTC+3


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _obj(cols, side: str) -> str:
    """json_object('a', NEW."a", …); пусто — '{}'."""
    if not cols:
        return "'{}'"
    return "json_object(" + ", ".join(f"{_lit(c)}, {side}.{_q(c)}" for c in cols) + ")"


def _event(table: str, op: str, keys, side_k: str, c_cols, c_side: str | None, o_cols, o_side: str | None) -> str:
    c = _obj(c_cols, c_side) if c_side else "NULL"
    o = _obj(o_cols, o_side) if o_side else "NULL"
    return (f"INSERT INTO jrn_events (e, t, op, k, c, o, at) SELECT m.epoch, {_lit(table)}, {_lit(op)}, "
            f"{_obj(keys, side_k)}, {c}, {o}, {_AT} {_CAPTURE};")


def _trig(name: str, when: str, body: str) -> str:
    return f"CREATE TEMP TRIGGER IF NOT EXISTS {name} {when} BEGIN {body} END"


def deny_statements(schema: dict[str, list[tuple[str, int]]]) -> list[str]:
    """Триггеры запрета: таблица → [(колонка, позиция в ключе или 0)] по порядку схемы."""
    out: list[str] = []
    if "jrn_meta" not in schema:
        return out                                    # схема старше эпохи — запрещать не по чему
    body = f"SELECT RAISE(ABORT, {_lit(_MSG)}) WHERE {_GUARD};"
    for table, spec in schema.items():
        if table not in TABLES or table == "server_state":
            continue
        cols = [c for c, _pk in spec]
        deny = denied_columns(table, cols)
        if not deny:
            continue
        t = _q(table)
        if len(deny) == len(cols):                    # строка целиком — решение: и вставка, и удаление
            out.append(_trig(f"jrn_deny_{table}_i", f"AFTER INSERT ON main.{t}", body))
            out.append(_trig(f"jrn_deny_{table}_d", f"AFTER DELETE ON main.{t}", body))
        changed = " OR ".join(f"OLD.{_q(c)} IS NOT NEW.{_q(c)}" for c in deny)
        of = ", ".join(_q(c) for c in deny)
        out.append(_trig(f"jrn_deny_{table}_u", f"AFTER UPDATE OF {of} ON main.{t} WHEN {changed}", body))
    if "server_state" in schema and STATE_DECISION:
        keys = ", ".join(_lit(k) for k in STATE_DECISION)
        out.append(_trig("jrn_deny_state_i", f"AFTER INSERT ON main.server_state WHEN NEW.key IN ({keys})", body))
        out.append(_trig("jrn_deny_state_u", f"AFTER UPDATE ON main.server_state WHEN (NEW.key IN ({keys}) OR "
                                             f"OLD.key IN ({keys})) AND (OLD.value IS NOT NEW.value OR "
                                             f"OLD.key IS NOT NEW.key)", body))
        out.append(_trig("jrn_deny_state_d", f"AFTER DELETE ON main.server_state WHEN OLD.key IN ({keys})", body))
    return out


def capture_statements(schema: dict[str, list[tuple[str, int]]], guest: bool = False) -> list[str]:
    """Триггеры захвата событий (нужны JSON-функции SQLite и таблица jrn_events)."""
    out: list[str] = []
    if "jrn_meta" not in schema or "jrn_events" not in schema:
        return out
    for table, spec in schema.items():
        if table not in TABLES or table == "server_state":
            continue
        cols = [c for c, _pk in spec]
        keys = [c for c, pk in sorted(spec, key=lambda x: x[1]) if pk]
        t = _q(table)
        if history_captured(table, guest):
            # история: дописка вместе с решением на резерве — основе (чистка по сроку — своя у каждой)
            body = _event(table, "I", [], "NEW", [c for c in cols if c not in keys], "NEW", [], None)
            out.append(_trig(f"jrn_cap_{table}_i", f"AFTER INSERT ON main.{t}", body))
            continue
        cap = captured_columns(table, cols, guest)
        if not cap:
            continue
        out.append(_trig(f"jrn_cap_{table}_i", f"AFTER INSERT ON main.{t}",
                         _event(table, "I", keys, "NEW", cap, "NEW", [], None)))
        out.append(_trig(f"jrn_cap_{table}_d", f"AFTER DELETE ON main.{t}",
                         _event(table, "D", keys, "OLD", [], None, cap, "OLD")))
        changed = " OR ".join(f"OLD.{_q(c)} IS NOT NEW.{_q(c)}" for c in cap)
        of = ", ".join(_q(c) for c in cap)
        out.append(_trig(f"jrn_cap_{table}_u", f"AFTER UPDATE OF {of} ON main.{t} WHEN {changed}",
                         _event(table, "U", keys, "OLD", cap, "NEW", cap, "OLD")))
    if "server_state" in schema and STATE_DECISION:
        keys = ", ".join(_lit(k) for k in STATE_DECISION)
        out.append(_trig("jrn_cap_state_i", f"AFTER INSERT ON main.server_state WHEN NEW.key IN ({keys})",
                         _event("server_state", "I", ["key"], "NEW", ["value"], "NEW", [], None)))
        out.append(_trig("jrn_cap_state_u", f"AFTER UPDATE ON main.server_state WHEN (NEW.key IN ({keys}) OR "
                                            f"OLD.key IN ({keys})) AND (OLD.value IS NOT NEW.value OR "
                                            f"OLD.key IS NOT NEW.key)",
                         _event("server_state", "U", ["key"], "NEW", ["value"], "NEW", ["value"], "OLD")))
        out.append(_trig("jrn_cap_state_d", f"AFTER DELETE ON main.server_state WHEN OLD.key IN ({keys})",
                         _event("server_state", "D", ["key"], "OLD", [], None, ["value"], "OLD")))
    return out


def statements(schema: dict[str, list[tuple[str, int]]], guest: bool = False) -> list[str]:
    """Все триггеры (запрет, затем захват) — для проверок и тестов."""
    return deny_statements(schema) + capture_statements(schema, guest)


def schema_of(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """Таблица → [(колонка, позиция в первичном ключе или 0)] по порядку схемы."""
    tables = [r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")]
    return {t: [(r[1], int(r[5])) for r in conn.execute(f"PRAGMA main.table_info({_q(t)})")] for t in tables}


_CAPTURE_FAILED: dict[str, str] = {}


def capture_failed() -> str:
    """Причина, по которой захват не встал (пусто — встал или не нужен): журнал тогда не объявляется."""
    return next(iter(_CAPTURE_FAILED.values()), "")


def install(conn: sqlite3.Connection, guest: bool = False) -> bool:
    """Поставить триггеры на соединение; False — схемы ещё нет (первый старт до
    init_schema): поставит следующий вызов после создания таблиц. Запрет ставится
    всегда, его ошибка — наружу; захват — только при JSON-функциях, его отказ —
    строкой журнала (ERROR, один раз) и отметкой capture_failed."""
    schema = schema_of(conn)
    deny = deny_statements(schema)
    if not deny:
        return False
    for s in deny:
        conn.execute(s)
    if not json_ok(conn):
        if "json" not in _CAPTURE_FAILED:
            log.warning("журнал: в SQLite нет JSON-функций — журнал правок не пишется, направление пойдёт прежним путём")
        _CAPTURE_FAILED.setdefault("json", "в SQLite нет JSON-функций")
        return True
    try:
        for s in capture_statements(schema, guest):
            conn.execute(s)
    except sqlite3.Error as e:
        rows = conn.execute("SELECT name FROM sqlite_temp_master WHERE type = 'trigger' AND name LIKE 'jrn_cap_%'")
        for (name,) in rows.fetchall():
            conn.execute(f"DROP TRIGGER IF EXISTS temp.{_q(name)}")      # частичный захват хуже никакого
        if "sql" not in _CAPTURE_FAILED:
            log.error("журнал: захват событий не поставлен: %s — журнал не объявляется", e)
        _CAPTURE_FAILED["sql"] = str(e)
    return True
