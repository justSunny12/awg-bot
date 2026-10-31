"""Правки по ревью — файлы резерва и кворума агента шлюза (domain/gateway/reserve/store.py,
quorum.py, util/fsatomic): чтение-изменение-запись reserve.json и quorum.json под одним
замком, подъём линка скриптом — вне замка с перечитыванием файла после, запись — своим
временным файлом на каждый вызов.

Обвязка — `gw`/`host` из test_gw_reserve_agent (база, rsvlink и запись DNS подменены);
потоки — настоящие, как у агента (такт, ответы зондов, слова резерва, документ).

Цена ошибки: два потока с общим временным файлом — битый JSON, агент читает пустой раздел
(«резерва нет»), пропадают эпоха e_known и обязательства «нет» — после перезапуска агент
голосует под прежней эпохой; применение документа, пока скрипт поднимает линк, записывает
состояние резерва, прочитанное до подъёма, — слово `state{serving}` резерва стёрто, и
пересылка закрывается при живом переезде.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from awgbot.infra.gwguard import rsv as rsvlink
from awgbot.util import fsatomic
from tests.unit.test_gw_reserve_agent import _checked, gw, host

pytestmark = pytest.mark.unit
__all__ = ["gw", "host"]


def _run_threads(*fns) -> None:
    start = threading.Barrier(len(fns))
    errs: list[BaseException] = []

    def wrap(fn):
        def go():
            try:
                start.wait()
                fn()
            except BaseException as e:                        # noqa: BLE001
                errs.append(e)
        return go
    ts = [threading.Thread(target=wrap(f)) for f in fns]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert not errs, errs


def test_parallel_writers_of_quorum_json_lose_nothing(gw):
    from tests.unit.test_gw_quorum import _sec
    gw.quorum_apply(_sec())

    def pledges(i):
        def go():
            for k in range(9):
                gw._pledge_store(f"awgq{i}x{k % 3}", {"e": 12, "at": "2026-10-31T10:00:00+03:00"})
        return go

    def epochs():
        for e in range(13, 25):
            gw._quorum_epoch_persist(e)
    _run_threads(*(pledges(i) for i in range(6)), epochs)
    st = json.loads(gw._QUO_STORE.read_text(encoding="utf-8"))
    assert len(st["pledges"]) == 18, f"обязательства потеряны параллельной записью: {sorted(st['pledges'])}"
    assert st["e_known"] == 24, "известная эпоха потеряна параллельной записью"
    assert not list(gw._QUO_STORE.parent.glob(".quorum.json.*.tmp")), "временные файлы остались"


def test_the_document_applied_while_the_link_comes_up_keeps_the_word_of_the_reserve(gw, host, monkeypatch):
    host.link = True
    entered = threading.Event()

    def slow_up(conf, admin_ips=None):
        entered.set()
        time.sleep(0.4)                                       # скрипт поднимает линк — секунды
        return host._up(conf, admin_ips)
    monkeypatch.setattr(rsvlink, "up", slow_up)

    def apply():
        gw.reserve_apply(_checked(gw.conf.replace("Jc = 5", "Jc = 6")))

    def word():
        entered.wait(5)
        gw.reserve_state_in("serving", 7)
    _run_threads(apply, word)
    sec = gw.reserve_section()
    assert (sec.get("reserve_state"), sec.get("reserve_move")) == ("serving", 7), \
        f"документ стёр слово резерва, пришедшее во время подъёма линка: {sec.get('reserve_state')}, {sec.get('reserve_move')}"
    assert "Jc = 6" in sec["conf"]


def test_parallel_private_writes_of_one_file_stay_whole(tmp_path):
    path = tmp_path / "reserve.json"
    big = {"k": "x" * 20000}

    def writer(n):
        def go():
            for i in range(12):
                fsatomic.write_private(path, json.dumps({**big, "n": n, "i": i}))
        return go
    _run_threads(*(writer(n) for n in range(4)))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["k"] == big["k"] and data["i"] == 11, "файл — смесь двух записей"
    assert not list(tmp_path.glob("*.tmp")), "временные файлы остались"
