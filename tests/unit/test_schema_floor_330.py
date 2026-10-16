"""
Минимум схемы — 3.3.0: колонка подсетей слота `gateways.lan_subnets`.

Переименование `home_subnets` → `lan_subnets` на старте снято вместе со
сдвигом минимума: хост ниже 3.3.0 сначала едет на 3.3.0, копия, снятая до
неё, не разворачивается — сторож минимума называет причину.
"""
import sqlite3

import pytest

from awgbot.infra.db import Database
from awgbot.infra.db.schema import SCHEMA, schema_gap

pytestmark = pytest.mark.unit


def _old_db(path: str) -> None:
    """База, как её создала 3.2.x: та же схема, но колонка подсетей под прежним
    именем; в слоте 1 — две подсети."""
    assert "lan_subnets" in SCHEMA and "home_subnets" not in SCHEMA
    con = sqlite3.connect(path)
    con.executescript(SCHEMA.replace("lan_subnets", "home_subnets"))
    con.execute("INSERT INTO gateways(id, device_id, link_if, link_port, link_cidr, preferred, "
                "home_subnets, label, created_at) VALUES (1, 7, 'awglink', 443, '10.99.99.0/30', 1, "
                "'192.168.1.0/24 10.20.0.0/16', '', '2026-01-01T00:00:00')")
    con.commit()
    con.close()


def test_a_database_below_the_floor_is_refused_by_the_floor_check(tmp_path):
    path = str(tmp_path / "old.db")
    _old_db(path)
    assert schema_gap(path) == "в таблице gateways нет колонки lan_subnets", (
        "копия 3.2.x обязана отбиваться сторожем минимума: переименования на старте больше нет")


def test_init_schema_no_longer_renames_the_old_column(tmp_path):
    """Шаг доводки снят: база с прежним именем не переписывается молча."""
    path = str(tmp_path / "old.db")
    _old_db(path)
    db = Database(path)
    try:
        db.init_schema()
        cols = {r[1] for r in db._connection().execute("PRAGMA table_info(gateways)")}
        assert "home_subnets" in cols and "lan_subnets" not in cols
    finally:
        db.close()


def test_a_fresh_database_is_created_under_the_new_name(tmp_path):
    db = Database(str(tmp_path / "new.db"))
    try:
        db.init_schema()
        cols = {r[1] for r in db._connection().execute("PRAGMA table_info(gateways)")}
        assert "lan_subnets" in cols and "home_subnets" not in cols
    finally:
        db.close()


def test_a_fresh_database_passes_the_floor_check(tmp_path):
    """Копия, снятая на 3.3.0, разворачивается."""
    path = str(tmp_path / "new.db")
    db = Database(path)
    db.init_schema()
    db.close()
    assert schema_gap(path) == ""
