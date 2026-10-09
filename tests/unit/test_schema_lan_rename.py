"""
3.2.x → 3.3.0: колонка `gateways.home_subnets` стала `lan_subnets`.

База прежней версии приходит двумя путями — накатом поверх хоста и
восстановлением копии; оба проходят через init_schema. Переименование — на
месте, данные те же; копия 3.2.x по-прежнему проходит сторож минимума.
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


def test_an_old_database_gets_the_column_renamed_with_its_data(tmp_path):
    path = str(tmp_path / "old.db")
    _old_db(path)
    assert schema_gap(path) == "", "копия 3.2.x обязана проходить сторож минимума"
    db = Database(path)
    try:
        db.init_schema()
        cols = {r[1] for r in db._connection().execute("PRAGMA table_info(gateways)")}
        assert "lan_subnets" in cols and "home_subnets" not in cols
        assert db.gateway(1).lan_subnets == ["192.168.1.0/24", "10.20.0.0/16"], "подсети слота потеряны"
        db.init_schema()                                     # повторный старт — без ошибки и без изменений
        assert db.gateway(1).lan_subnets == ["192.168.1.0/24", "10.20.0.0/16"]
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


def test_the_renamed_database_still_passes_the_floor_check(tmp_path):
    """Копия, снятая на 3.3.0, разворачивается: сторож минимума не требует
    прежнего имени колонки."""
    path = str(tmp_path / "new.db")
    db = Database(path)
    db.init_schema()
    db.close()
    assert schema_gap(path) == ""
