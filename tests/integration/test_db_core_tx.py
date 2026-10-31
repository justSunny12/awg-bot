"""
Ядро базы: транзакция потока и кэш горячих ключей состояния.

Стережём:
- close() посреди чужой (или своей) транзакции не подменяет ей соединение:
  следующий запрос того же потока шёл в новое соединение и ждал замок записи,
  который держит его же транзакция, — 5 с и «database is locked», запись
  терялась откатом;
- кэш состояния переезда сбрасывается на любом коммите, а не только в
  set_state: запись сырым SQL в транзакции, применение событий держателя и
  перенос хвоста иначе оставляли видимость двойников по старому значению до
  следующей записи через set_state.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from awgbot.core import config
from awgbot.infra.db.core import MIGRATION_RUNNING, MIGRATION_STATE_KEY

pytestmark = pytest.mark.integration


def _put(cur, key: str, value: str) -> None:
    cur.execute("INSERT INTO server_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# ── close() посреди транзакции ───────────────────────────────────────────────

def test_close_from_another_thread_does_not_split_a_running_transaction(db):
    inside, closed, done = threading.Event(), threading.Event(), {}

    def worker():
        try:
            with db.transaction(), db._tx() as c:
                _put(c, "t_a", "1")
                inside.set()
                closed.wait(5)
                t0 = time.monotonic()
                with db._tx() as c2:                        # тот же поток, та же транзакция
                    _put(c2, "t_b", "2")
                done["dt"] = time.monotonic() - t0
        except Exception as e:                              # noqa: BLE001
            done["err"] = e

    th = threading.Thread(target=worker)
    th.start()
    assert inside.wait(5)
    db.close()                                              # хранилище соединений подменено
    closed.set()
    th.join(15)
    assert "err" not in done, done.get("err")
    assert done["dt"] < 1.0                                 # не ждал собственный замок
    assert db.get_state("t_a") == "1" and db.get_state("t_b") == "2"


def test_close_inside_own_transaction_closes_after_it(db):
    with db.transaction(), db._tx() as c:
        _put(c, "t_c", "1")
        before = db._connection()
        db.close()
        assert db._connection() is before                   # до конца транзакции — её соединение
        with db._tx() as c2:
            _put(c2, "t_d", "2")
    assert db._connection() is not before                   # после — новое, прежнее закрыто
    assert db.get_state("t_c") == "1" and db.get_state("t_d") == "2"


# ── кэш горячего ключа ───────────────────────────────────────────────────────

@pytest.fixture()
def mig_cfg(monkeypatch):
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.0")


def test_raw_write_in_a_transaction_refreshes_migration_visibility(db, mig_cfg):
    assert db.migration_visibility_running() is False       # кэш: пусто
    with db._tx() as c:
        _put(c, MIGRATION_STATE_KEY, MIGRATION_RUNNING)
    assert db.migration_visibility_running() is True
    with db._tx() as c:
        c.execute("DELETE FROM server_state WHERE key = ?", (MIGRATION_STATE_KEY,))
    assert db.migration_visibility_running() is False


@pytest.mark.jrn_raw("подменённый применитель пишет мимо захвата")
def test_journal_apply_commit_refreshes_migration_visibility(db, mig_cfg, monkeypatch):
    from awgbot.util.rsvjrn import apply as _apply

    def fake_apply(conn, events):                           # коммит мимо _tx, как у применителя
        _put(conn, MIGRATION_STATE_KEY, MIGRATION_RUNNING)
        conn.commit()

    assert db.migration_visibility_running() is False
    monkeypatch.setattr(_apply, "apply", fake_apply)
    db.jrn_apply([])
    assert db.migration_visibility_running() is True


@pytest.mark.jrn_raw("коммит другого потока сырым соединением")
def test_a_read_before_the_commit_is_not_cached_after_it(db, mig_cfg):
    """Поток прочёл базу до коммита, кладёт в кэш после сброса — старое
    значение в кэш не попадает."""
    real = db._connection

    class _Late:
        def __init__(self, conn):
            self._c = conn

        def execute(self, *a):
            row = self._c.execute(*a).fetchone()
            other = sqlite3.connect(db.path)                # коммит другого потока между чтением и кэшем
            _put(other, MIGRATION_STATE_KEY, MIGRATION_RUNNING)
            other.commit()
            other.close()
            db._hot_state_drop()
            return _Rows(row)

    class _Rows:
        def __init__(self, row):
            self._r = row

        def fetchone(self):
            return self._r

    db.__dict__["_connection"] = lambda: _Late(real())
    try:
        assert db.get_state(MIGRATION_STATE_KEY) is None    # прочитано до коммита
    finally:
        del db.__dict__["_connection"]
    assert db.get_state(MIGRATION_STATE_KEY) == MIGRATION_RUNNING
