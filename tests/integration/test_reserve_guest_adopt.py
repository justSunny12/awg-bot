"""Подхват реплики при старте бота основы на резерве
(domain/services/reserve/guest.py: guest_adopt_snapshot): при переезде кнопкой
основа отдаёт финальную реплику, гость перезапускается и ставит её вместо
базы из пакета.

Цена ошибки: реплика, не подхваченная, — бот на резерве поднимается на базе
из пакета двухчасовой давности, и всё, что клиенты сделали за это время
(платежи, новые устройства), пропадает при переезде; подхват старой реплики
поверх более новой базы — откат на часы назад при каждом перезапуске гостя;
повторный подхват той же реплики на каждом старте — база затирается
исходной и правки на резерве теряются; ключ или реплика, не прошедшие
проверку, но молча принятые, — чужие данные в базе бота.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from awgbot.core import config
from awgbot.infra import rsvzone
from awgbot.util import timeutil
from awgbot.util.rsvpkg import seal

pytestmark = pytest.mark.integration

K = bytes([7]) * 32


def _db_bytes(tmp_path, marker: str) -> bytes:
    path = tmp_path / f"{marker}.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE marker (v TEXT)")
    con.execute("INSERT INTO marker VALUES (?)", (marker,))
    con.commit()
    con.close()
    return path.read_bytes()


def _marker(path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT v FROM marker").fetchone()[0]
    finally:
        con.close()


@pytest.fixture()
def store(services, tmp_path, monkeypatch):
    run = tmp_path / "run"
    (run / "data").mkdir(parents=True)
    monkeypatch.setattr(rsvzone, "RUN_DIR", run)
    monkeypatch.setattr(rsvzone, "K_FILE", run / "k")
    monkeypatch.setattr(rsvzone, "REP_PATH", tmp_path / "var" / "rep.bin")
    monkeypatch.setattr(config, "DB_PATH", str(run / "data" / "bot.db"))
    rsvzone.write_private(run / "k", K.hex())
    (run / "data" / "bot.db").write_bytes(_db_bytes(tmp_path, "package"))
    rsvzone.write_private(run / "pkg.json", json.dumps({"pkg_gen": 3, "db_at": "2026-10-21T10:00:00+00:00"}))
    return type("S", (), {"svc": services, "run": run, "tmp": tmp_path})()


def _put_rep(store, marker: str, at: str, *, k: bytes = K, db: bytes | None = None) -> None:
    blob = seal.snapshot_seal(k, {"pkg_gen": 3, "at": at, "db": (db if db is not None else _db_bytes(store.tmp, marker)).hex()})
    rsvzone.write_private(rsvzone.REP_PATH, blob)


def test_a_newer_replica_replaces_the_database_and_the_old_one_is_kept_aside(store):
    _put_rep(store, "replica", "2026-10-21T11:00:00+00:00")
    (store.run / "data" / "bot.db-wal").write_bytes(b"w")
    (store.run / "data" / "bot.db-shm").write_bytes(b"s")
    assert store.svc.guest_adopt_snapshot() is True
    assert _marker(store.run / "data" / "bot.db") == "replica"
    assert _marker(store.run / "data" / "bot.db.prev") == "package", "прежнюю базу негде восстановить"
    assert not (store.run / "data" / "bot.db-wal").exists() and not (store.run / "data" / "bot.db-shm").exists(), \
        "хвосты журнала прежней базы испортили бы новую"
    meta = json.loads((store.run / "pkg.json").read_text())
    assert meta["db_at"] == "2026-10-21T11:00:00+00:00" and meta["pkg_gen"] == 3, "метки пакета затёрты"
    assert (os.stat(store.run / "data" / "bot.db").st_mode & 0o777) == 0o600


def test_the_same_replica_is_adopted_once(store):
    _put_rep(store, "replica", "2026-10-21T11:00:00+00:00")
    assert store.svc.guest_adopt_snapshot() is True
    con = sqlite3.connect(store.run / "data" / "bot.db")
    con.execute("INSERT INTO marker VALUES ('правка на резерве')")
    con.commit()
    con.close()
    assert store.svc.guest_adopt_snapshot() is False, "реплика подхвачена второй раз — правки на резерве затёрты"
    con = sqlite3.connect(store.run / "data" / "bot.db")
    assert con.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 2
    con.close()


@pytest.mark.parametrize("at", ["2026-10-21T10:00:00+00:00", "2026-10-21T09:00:00+00:00"])
def test_a_replica_not_newer_than_the_database_is_left_alone(store, at):
    _put_rep(store, "replica", at)
    assert store.svc.guest_adopt_snapshot() is False
    assert _marker(store.run / "data" / "bot.db") == "package"


def test_without_a_replica_or_a_key_nothing_happens(store):
    assert store.svc.guest_adopt_snapshot() is False
    _put_rep(store, "replica", "2026-10-21T11:00:00+00:00")
    (store.run / "k").unlink()
    assert store.svc.guest_adopt_snapshot() is False
    rsvzone.write_private(store.run / "k", "не hex")
    assert store.svc.guest_adopt_snapshot() is False
    assert _marker(store.run / "data" / "bot.db") == "package"


@pytest.mark.parametrize("what", ["other_key", "garbage", "empty_db", "bad_hex"])
def test_a_replica_that_does_not_pass_the_checks_is_not_adopted(store, what):
    if what == "other_key":
        _put_rep(store, "replica", "2026-10-21T11:00:00+00:00", k=bytes([9]) * 32)
    elif what == "garbage":
        rsvzone.write_private(rsvzone.REP_PATH, b"\x00" * 200)
    elif what == "empty_db":
        blob = seal.snapshot_seal(K, {"pkg_gen": 3, "at": "2026-10-21T11:00:00+00:00", "db": ""})
        rsvzone.write_private(rsvzone.REP_PATH, blob)
    else:
        blob = seal.snapshot_seal(K, {"pkg_gen": 3, "at": "2026-10-21T11:00:00+00:00", "db": "zz"})
        rsvzone.write_private(rsvzone.REP_PATH, blob)
    assert store.svc.guest_adopt_snapshot() is False
    assert _marker(store.run / "data" / "bot.db") == "package", "негодная реплика затёрла базу"


def test_a_missing_marker_in_the_meta_means_any_replica_is_newer(store):
    rsvzone.write_private(store.run / "pkg.json", json.dumps({"pkg_gen": 3}))
    _put_rep(store, "replica", timeutil.now_iso())
    assert store.svc.guest_adopt_snapshot() is True
