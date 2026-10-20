"""Таблицы резерва в базе (infra/db/reserve.py, schema/ddl.py): строка пары
и месячный итог трафика через резерв.

Цена ошибки: вторая строка пары рядом с первой — бот не знает, с кем он в
паре, и слушает канал не того сервера; точечная правка, которая трогает
порт или подсеть линка в обход пересоздания, — линк пары и база расходятся;
повторное закрытие месяца, которое дописывает строку, — месячный трафик
через резерв удваивается в отчёте.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _set(db, **over):
    kw = dict(name="Сервер 2", host="srv-b.example.net", pair_if="awgpair", pair_port=51830, peer_port=51831,
              pair_cidr="10.99.98.0/30", listener=True)
    kw.update(over)
    return db.reserve_peer_set(**kw)


def test_no_pair_reads_as_none(db):
    assert db.reserve_peer() is None


def test_the_pair_row_reads_back_with_defaults(db):
    _set(db)
    p = db.reserve_peer()
    assert {k: p[k] for k in ("name", "host", "pair_if", "pair_port", "peer_port", "pair_cidr", "listener",
                              "bot_username", "peer_admin_id")} == {
        "name": "Сервер 2", "host": "srv-b.example.net", "pair_if": "awgpair", "pair_port": 51830,
        "peer_port": 51831, "pair_cidr": "10.99.98.0/30", "listener": 1, "bot_username": "", "peer_admin_id": 0}
    assert p["created_at"], "время создания пары не записано"


def test_setting_the_pair_again_replaces_the_row_and_does_not_add_one(db):
    _set(db)
    _set(db, host="198.51.100.7", listener=False, bot_username="srv_b_bot", peer_admin_id=777)
    rows = db._connection().execute("SELECT COUNT(*) FROM reserve_peer").fetchone()[0]
    assert rows == 1, "в первой версии пара одна"
    p = db.reserve_peer()
    assert (p["host"], p["listener"], p["bot_username"], p["peer_admin_id"]) == ("198.51.100.7", 0, "srv_b_bot", 777)


def test_a_point_update_touches_only_what_it_may(db):
    _set(db)
    db.reserve_peer_update(name="Сервер Б", host="203.0.113.5", peer_port=51900, pair_port=1, pair_cidr="0.0.0.0/0",
                           listener=0)
    p = db.reserve_peer()
    assert (p["name"], p["host"], p["peer_port"]) == ("Сервер Б", "203.0.113.5", 51900)
    assert (p["pair_port"], p["pair_cidr"], p["listener"]) == (51830, "10.99.98.0/30", 1), \
        "свой порт, подсеть и сторона канала меняются только пересозданием пары"
    db.reserve_peer_update(unknown=1)                         # ничего разрешённого — ничего не делает
    assert db.reserve_peer()["name"] == "Сервер Б"


def test_deleting_the_pair_leaves_nothing(db):
    _set(db)
    db.reserve_peer_delete()
    assert db.reserve_peer() is None
    db.reserve_peer_delete()                                  # повтор — не ошибка


def test_closing_a_month_twice_replaces_its_row(db):
    db.reserve_traffic_archive("2026-09", "out", 100, 10)
    db.reserve_traffic_archive("2026-09", "out", 150, 15)
    db.reserve_traffic_archive("2026-09", "in", 7, 3)
    rows = db.reserve_traffic_months()
    assert [(r["month"], r["dir"], r["rx"], r["tx"]) for r in rows] == [("2026-09", "in", 7, 3),
                                                                        ("2026-09", "out", 150, 15)]
    assert all(r["archived_at"] for r in rows)


def test_months_come_newest_first_and_are_limited(db):
    for m in ("2026-07", "2026-09", "2026-08", "2026-10"):
        db.reserve_traffic_archive(m, "out", 1, 1)
    assert [r["month"] for r in db.reserve_traffic_months(limit=3)] == ["2026-10", "2026-09", "2026-08"]
