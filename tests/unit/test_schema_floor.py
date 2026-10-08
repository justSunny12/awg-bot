"""Минимум 3.2.0: миграций ниже в коде нет, копия со старой схемой не
разворачивается — ни у бота (check_backup), ни у CLI (tools.check_backup)."""
import io
import json
import sqlite3
import tarfile

from awgbot.infra.db.schema import schema_gap


def _old_db(path, *, missing: str = "clients.kind") -> None:
    table, col = missing.split(".")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE clients(id INTEGER PRIMARY KEY, kind TEXT, tg_name TEXT, tg_name_at TEXT, tg_username TEXT);
        CREATE TABLE devices(id INTEGER PRIMARY KEY, holder_client_id INTEGER, is_gateway INTEGER DEFAULT 0);
        CREATE TABLE gateways(id INTEGER PRIMARY KEY, lan_mode INTEGER);
        CREATE TABLE device_traffic(device_id INTEGER, rf_rx_month INTEGER, rf_tx_month INTEGER);
    """)
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})") if r[1] != col]
    con.executescript(f"CREATE TABLE t2 AS SELECT {', '.join(cols)} FROM {table}; "
                      f"DROP TABLE {table}; ALTER TABLE t2 RENAME TO {table};")
    con.commit(); con.close()


def test_schema_gap_names_the_first_missing_column(tmp_path):
    p = str(tmp_path / "old.db")
    _old_db(p)
    assert schema_gap(p) == "в таблице clients нет колонки kind"
    _old_db(str(tmp_path / "rf.db"), missing="device_traffic.rf_tx_month")
    assert schema_gap(str(tmp_path / "rf.db")) == "в таблице device_traffic нет колонки rf_tx_month"


def test_schema_gap_is_empty_on_a_current_database(services):
    """База, созданная текущей SCHEMA, минимуму соответствует; таблиц,
    которых нет вовсе, сторож не требует."""
    assert schema_gap(services.db.path) == ""


def test_schema_gap_catches_old_flags_and_leftover_secret_column(tmp_path):
    p = str(tmp_path / "flag.db")
    con = sqlite3.connect(p)
    con.executescript("CREATE TABLE devices(id INTEGER PRIMARY KEY, holder_client_id INTEGER, is_gateway INTEGER);"
                      "INSERT INTO devices VALUES (1, NULL, 1);")
    con.commit(); con.close()
    assert "is_gateway" in schema_gap(p)
    q = str(tmp_path / "secret.db")
    con = sqlite3.connect(q)
    con.executescript("CREATE TABLE devices(id INTEGER PRIMARY KEY, holder_client_id INTEGER, full_access_link TEXT);")
    con.commit(); con.close()
    assert "full_access_link" in schema_gap(q)
    (tmp_path / "junk.db").write_bytes(b"not a database")
    assert schema_gap(str(tmp_path / "junk.db")) == "не база SQLite"


def _archive(db_bytes: bytes, role: str = "main") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, raw):
            ti = tarfile.TarInfo(name); ti.size = len(raw)
            tar.addfile(ti, io.BytesIO(raw))
        add("state/backup-meta.json", json.dumps({"role": role, "created_at": "2026-10-01T12:00:00+03:00"}).encode())
        add("state/bot.db", db_bytes)
    return buf.getvalue()


def test_bot_refuses_a_copy_with_a_pre_floor_schema(services, tmp_path):
    p = tmp_path / "old.db"
    _old_db(str(p))
    res = services.inspect_backup(_archive(p.read_bytes()), "awg-bot_old.tgz")
    assert res["ok"] is False and "ниже 3.2.0" in res["error"] and "clients" in res["error"], res


def test_bot_accepts_a_copy_with_a_current_schema(services, tmp_path):
    res = services.inspect_backup(_archive(services.db_snapshot_bytes(services.db.path)), "awg-bot_new.tgz")
    assert res["ok"] is True, res


def test_cli_check_backup_exit_codes(tmp_path, capsys):
    from tools import check_backup
    p = str(tmp_path / "old.db")
    _old_db(p)
    assert check_backup.main(["x", p]) == 1
    assert "ниже минимума 3.2.0" in capsys.readouterr().err
    good = str(tmp_path / "good.db")
    con = sqlite3.connect(good); con.executescript("CREATE TABLE clients(id INTEGER, kind TEXT, tg_name TEXT, tg_name_at TEXT, tg_username TEXT);"); con.close()
    assert check_backup.main(["x", good]) == 0
