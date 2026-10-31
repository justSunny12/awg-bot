"""
Списки в одной строке базы (CSV меток трафика и порогов, id служебных
сообщений чата) — чтение и запись под одним замком.

Стережём: метки и id читались вне транзакции и писались потом целиком —
два потока (тик лимитов и кнопка админа; два хопа экрана одного чата)
затирали друг другу добавленное: уведомление «трафик исчерпан» приходило
второй раз, служебное сообщение не убиралось при возврате в меню.
"""
from __future__ import annotations

import json
import threading

import pytest

pytestmark = pytest.mark.integration


def _interleave(first, second, monkeypatch, target, attr):
    """first читает и замирает после первого чтения; second идёт целиком;
    first дописывает. С замком second ждёт first — итог: оба добавления."""
    paused, go = threading.Event(), threading.Event()
    real = getattr(target, attr)
    seen = {"n": 0}

    def slow(*a, **k):
        out = real(*a, **k)
        if threading.current_thread().name == "first" and not seen["n"]:
            seen["n"] = 1
            paused.set()
            go.wait(5)
        return out
    monkeypatch.setattr(target, attr, slow)
    a = threading.Thread(target=first, name="first")
    a.start()
    assert paused.wait(5)
    b = threading.Thread(target=second, name="second")
    b.start()
    b.join(0.5)
    go.set()
    a.join(10)
    b.join(10)


def test_content_ids_from_two_threads_are_both_kept(db, monkeypatch):
    db.add_content_msg_id(1, 10)
    _interleave(lambda: db.add_content_msg_id(1, 11), lambda: db.add_content_msg_id(1, 12),
                monkeypatch, json, "loads")
    assert sorted(db.pop_content_msg_ids(1)) == [10, 11, 12]


def test_traffic_markers_from_two_threads_are_both_kept(db, monkeypatch):
    cid = db.create_client("A", 1, "s", "e", "Cr0000000000")
    _interleave(lambda: db.add_traffic_notified(cid, "cli80"), lambda: db.add_traffic_notified(cid, "dev80:1"),
                monkeypatch, db, "get_traffic_notified")
    assert db.get_traffic_notified(cid) == {"cli80", "dev80:1"}


def test_thresholds_from_two_threads_are_both_kept(db, monkeypatch):
    cid = db.create_client("B", 1, "s", "e", "Cs0000000000")
    _interleave(lambda: db.add_notified(cid, 1440), lambda: db.add_notified(cid, 10080),
                monkeypatch, db, "get_notified")
    assert db.get_notified(cid) == {1440, 10080}
