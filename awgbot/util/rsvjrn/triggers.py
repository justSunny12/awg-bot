"""
triggers.py — часть пакета rsvjrn (см. __init__): временные триггеры запрета
записи решений у следующего.

Ставятся на каждое соединение (`CREATE TEMP TRIGGER … ON main.<таблица>`):
живут, пока живо соединение, в файле базы их нет. Отказ — когда
`jrn_meta.role` не `holder` и это не транзакция применения (`applying = 0`;
применитель ставит 1 и снимает в той же транзакции — другие соединения отметки
не видят). `UPDATE OF <колонки решений>` не срабатывает на запись счётчиков
опросом, условие `IS NOT` — на запись того же значения. Сырые соединения
(реплика, бэкап, восстановление, слияние при возврате) триггеров не имеют.
"""
from __future__ import annotations

import sqlite3

from . import REFUSAL_PREFIX
from .classes import STATE_DECISION, TABLES, denied_columns

_MSG = f"{REFUSAL_PREFIX} эпоха не у этого процесса — решения пишет держатель"
_GUARD = ("(SELECT role FROM main.jrn_meta WHERE id = 1) <> 'holder' "
          "AND (SELECT applying FROM main.jrn_meta WHERE id = 1) = 0")


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _raise_body() -> str:
    return f"BEGIN SELECT RAISE(ABORT, '{_MSG}') WHERE {_GUARD}; END"


def statements(schema: dict[str, list[str]]) -> list[str]:
    """CREATE TEMP TRIGGER для таблиц схемы (таблица → колонки по порядку)."""
    out: list[str] = []
    body = _raise_body()
    if "jrn_meta" not in schema:
        return out                                    # схема старше эпохи — запрещать не по чему
    for table, cols in schema.items():
        if table not in TABLES or table == "server_state":
            continue
        deny = denied_columns(table, cols)
        if not deny:
            continue
        t = _q(table)
        whole = len(deny) == len(cols)
        if whole:                                     # строка целиком — решение: и вставка, и удаление
            out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_deny_{table}_i AFTER INSERT ON main.{t} {body}")
            out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_deny_{table}_d AFTER DELETE ON main.{t} {body}")
        changed = " OR ".join(f"OLD.{_q(c)} IS NOT NEW.{_q(c)}" for c in deny)
        of = ", ".join(_q(c) for c in deny)
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_deny_{table}_u AFTER UPDATE OF {of} ON main.{t} "
                   f"WHEN {changed} {body}")
    if "server_state" in schema and STATE_DECISION:
        keys = ", ".join("'" + k.replace("'", "''") + "'" for k in STATE_DECISION)
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_deny_state_i AFTER INSERT ON main.server_state "
                   f"WHEN NEW.key IN ({keys}) {body}")
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_deny_state_u AFTER UPDATE ON main.server_state "
                   f"WHEN (NEW.key IN ({keys}) OR OLD.key IN ({keys})) AND (OLD.value IS NOT NEW.value OR OLD.key IS NOT NEW.key) {body}")
        out.append(f"CREATE TEMP TRIGGER IF NOT EXISTS jrn_deny_state_d AFTER DELETE ON main.server_state "
                   f"WHEN OLD.key IN ({keys}) {body}")
    return out


def schema_of(conn: sqlite3.Connection) -> dict[str, list[str]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")]
    return {t: [r[1] for r in conn.execute(f"PRAGMA main.table_info({_q(t)})")] for t in tables}


def install(conn: sqlite3.Connection) -> bool:
    """Поставить триггеры на соединение; False — схемы ещё нет (первый старт до
    init_schema): поставит следующий вызов после создания таблиц."""
    stmts = statements(schema_of(conn))
    if not stmts:
        return False
    for s in stmts:
        conn.execute(s)
    return True
