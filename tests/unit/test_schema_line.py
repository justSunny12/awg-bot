"""Инвариант линии совместимости: в пределах COMPAT_FLOOR схема базы и conf
только растут.

Откат обновления в той же установке, восстановление бэкапа на выпуск ниже
текущего и шлюзы, идущие за сервером вниз, ставят прежний код на базу и conf,
которые уже тронул новый. Это работает, только если ни одна таблица и колонка
не пропала, не переименована и не сменила тип, а ключ conf не пропал и не
сменил тип: добавлять можно, убирать и менять — только вместе с подъёмом
COMPAT_FLOOR (docs/RELEASING.md §2: «колонка, добавленная в пределах линии,
сдвига не требует»).

Сторож — снимок на COMPAT_FLOOR в tests/fixtures: schema_floor.sql (DDL
свежей базы) и conf_floor.txt (ключи шаблонов conf/*.yaml с типом значения).
Текущее обязано быть их надмножеством. Снимок переснимается только вместе со
сдвигом COMPAT_FLOOR: `python -m tests.unit.test_schema_line`.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SCHEMA_FLOOR = FIXTURES / "schema_floor.sql"
CONF_FLOOR = FIXTURES / "conf_floor.txt"
_HEAD = re.compile(r"^-- COMPAT_FLOOR (\S+)$", re.M)


def _floor() -> str:
    from awgbot.core.compat import COMPAT_FLOOR
    return COMPAT_FLOOR


# ── схема базы ──────────────────────────────────────────────────────────────

def _current_ddl(tmp: Path) -> list[str]:
    """DDL таблиц свежей базы — той, что создаёт код (обе роли — одна схема)."""
    from awgbot.infra.db import Database
    db = Database(str(tmp / "floor.db"))
    try:
        db.init_schema()
        rows = db._connection().execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name").fetchall()
    finally:
        db.close()
    return [r[0].strip() for r in rows]


def _columns(ddl: str) -> dict[str, dict[str, str]]:
    """{таблица: {колонка: тип}} по тексту DDL — его разбирает сам SQLite."""
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(ddl)
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {t: {c[1]: str(c[2]).upper() for c in con.execute(f'PRAGMA table_info("{t}")')}
                for t in tables}
    finally:
        con.close()


def test_the_schema_only_grows_within_the_line(tmp_path):
    snap = SCHEMA_FLOOR.read_text(encoding="utf-8")
    m = _HEAD.search(snap)
    assert m, "в снимке нет строки «-- COMPAT_FLOOR X.Y.Z»"
    assert m.group(1) == _floor(), (
        f"COMPAT_FLOOR сдвинут ({m.group(1)} → {_floor()}): переснять снимок — "
        "python -m tests.unit.test_schema_line")
    floor, cur = _columns(snap), _columns(";\n".join(_current_ddl(tmp_path)) + ";")
    assert floor, "снимок пуст"
    gone = sorted(set(floor) - set(cur))
    assert not gone, f"таблицы пропали в линии {_floor()}: {gone} — это сдвиг COMPAT_FLOOR"
    for table, cols in floor.items():
        for col, typ in cols.items():
            assert col in cur[table], f"{table}.{col} пропала в линии {_floor()} — это сдвиг COMPAT_FLOOR"
            assert cur[table][col] == typ, (
                f"{table}.{col}: тип {typ} → {cur[table][col]} в линии {_floor()} — это сдвиг COMPAT_FLOOR")


# ── conf ────────────────────────────────────────────────────────────────────

def _type(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    return {int: "int", float: "float", str: "str", list: "list", dict: "dict"}.get(type(v), type(v).__name__)


def _flat(node, prefix: str = ""):
    if isinstance(node, dict) and node:
        for k, v in node.items():
            yield from _flat(v, f"{prefix}.{k}" if prefix else str(k))
    elif prefix:
        yield prefix, _type(node)


def _current_conf() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for f in sorted((ROOT / "conf").glob("*.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for key, typ in _flat(data):
            out[(f.name, key)] = typ
    return out


def _read_conf_floor() -> tuple[str, dict[tuple[str, str], str]]:
    text = CONF_FLOOR.read_text(encoding="utf-8")
    m = _HEAD.search(text)
    assert m, "в снимке conf нет строки «-- COMPAT_FLOOR X.Y.Z»"
    keys = {}
    for ln in text.splitlines():
        if ln and not ln.startswith("--"):
            name, key, typ = ln.split("\t")
            keys[(name, key)] = typ
    return m.group(1), keys


def test_conf_keys_only_grow_within_the_line():
    """Ключ пропал или сменил тип — прежний код прочтёт conf нового не так.
    null (пустое значение в шаблоне) — «не задано», совместим с любым типом."""
    ver, floor = _read_conf_floor()
    assert ver == _floor(), (
        f"COMPAT_FLOOR сдвинут ({ver} → {_floor()}): переснять снимок — python -m tests.unit.test_schema_line")
    assert floor, "снимок conf пуст"
    cur = _current_conf()
    gone = sorted(f"{n}: {k}" for n, k in floor if (n, k) not in cur)
    assert not gone, f"ключи conf пропали в линии {_floor()}: {gone} — это сдвиг COMPAT_FLOOR"
    changed = sorted(f"{n}: {k} {t} → {cur[(n, k)]}" for (n, k), t in floor.items()
                     if "null" not in (t, cur[(n, k)]) and cur[(n, k)] != t)
    assert not changed, f"ключи conf сменили тип в линии {_floor()}: {changed} — это сдвиг COMPAT_FLOOR"


def test_the_guard_sees_a_dropped_column_and_a_changed_type():
    """Сторож сам по себе: пропажу и смену типа он видит."""
    floor = _columns("CREATE TABLE t (a INTEGER, b TEXT);")
    assert set(floor["t"]) - set(_columns("CREATE TABLE t (a INTEGER);")["t"]) == {"b"}
    assert _columns("CREATE TABLE t (a TEXT, b TEXT);")["t"]["a"] != floor["t"]["a"]
    assert dict(_flat({"a": {"b": 1, "c": None}, "d": [1]})) == {"a.b": "int", "a.c": "null", "d": "list"}


# ── переснять снимок (только со сдвигом COMPAT_FLOOR) ──────────────────────

def _write_snapshots() -> None:
    import tempfile
    FIXTURES.mkdir(parents=True, exist_ok=True)
    head = (f"-- COMPAT_FLOOR {_floor()}\n"
            "-- Снимок на версию совместимости (tests/unit/test_schema_line.py): переснимается\n"
            "-- только вместе со сдвигом COMPAT_FLOOR — python -m tests.unit.test_schema_line\n")
    with tempfile.TemporaryDirectory() as d:
        ddl = _current_ddl(Path(d))
    SCHEMA_FLOOR.write_text(head + "\n" + ";\n\n".join(ddl) + ";\n", encoding="utf-8")
    lines = [f"{n}\t{k}\t{t}" for (n, k), t in sorted(_current_conf().items())]
    CONF_FLOOR.write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    _write_snapshots()
    sys.exit(0)
