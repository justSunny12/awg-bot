"""
Смена ключей линка на шлюзе: ответ сервера закрепляет выбор; клиент канала —
link_next, link_go, link_cancel, link_done, частый такт и такт аплинка.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from awgbot.domain.gateway.cfg import rekey as gwrekey
from awgbot.runtime import linkclient
from tests.unit import _link_txn_common as _c
from tests.unit._link_txn_common import (
    BACK_NOTE, NONE_ANSWERED, QUIET, _Client, _msg, _next_conf, _ready, _switched, _tries,
)

pytestmark = pytest.mark.unit

conf_dir, svc, clock, host = _c.conf_dir, _c.svc, _c.clock, _c.host


# ── ответ сервера закрепляет выбор ───────────────────────────────────────────

def test_a_server_answer_after_the_switch_fixes_the_new_keys(svc, host, clock):
    """Первое подписанное слово сервера в новой сессии на новых ключах:
    link_done ok, «✅ Линк на новых ключах» правкой приглашения, прежний
    конфиг стёрт."""
    _ready(svc, host)
    st = host.txn(); st["msg"] = [555, 42]
    (host.etc / "link.txn").write_text(json.dumps(st), encoding="utf-8")
    svc.rekey_go({"id": "a1b2c3d4e5f6"})
    res = svc.rekey_server_answered()
    assert res == {"done": {"id": "a1b2c3d4e5f6", "ok": True, "keys": "new"}, "note": "✅ Линк на новых ключах",
                   "msg": [555, 42]}
    # файлы перехода стёрты; осталась метка выпуска — момент смены ключей:
    # файл конфигурации, выпущенный раньше, несёт прежние ключи и получит отказ
    assert host.txn() == {} and host.etc_files() == {"issued_at"}
    assert int((host.etc / "issued_at").read_text(encoding="utf-8")) == int(clock.now)
    assert svc.bundle_issued_applied() == int(clock.now)
    assert svc.rekey_server_answered() == NONE_ANSWERED, "итог повторился на следующей сессии"


def test_a_server_answer_during_the_search_names_the_keys_that_work(svc, host, clock):
    """В поиске: ответ пришёл на новых (current next) — «новые», с
    уведомлением; на прежних — link_done keys old без уведомления (о
    возврате человек уже знает)."""
    _switched(svc, host, clock)
    clock.at(100)
    svc.rekey_tick()                                      # back, current prev
    res = svc.rekey_server_answered()
    assert res == {"done": {"id": "a1b2c3d4e5f6", "ok": False, "keys": "old"}, "note": "", "msg": None}, res
    assert host.txn() == {} and host.etc_files() == set()

    _switched(svc, host, clock, rid="c3c3c3c3c3c3")
    clock.at(300)
    host.alive = False
    svc.rekey_tick()                                      # back
    _tries(svc, host, clock, until=300 + gwrekey.SEARCH_FIRST + 2 * gwrekey.DEAD_CONFIRM_SECONDS)
    assert host.txn()["current"] == "next"
    res = svc.rekey_server_answered()
    assert res["done"] == {"id": "c3c3c3c3c3c3", "ok": True, "keys": "new"} and res["note"] == "✅ Линк на новых ключах"


def test_the_live_config_not_the_record_decides_which_keys_work(svc, host, clock):
    """Запись говорит «переключились», а на линке стоит прежний конфиг
    (сбой посреди применения): первое слово сервера закрепляет ПРЕЖНИЕ —
    «✅ новые» здесь было бы ложью, а link_done keys new увёл бы ВПС."""
    _switched(svc, host, clock)
    host.link.write_text((host.etc / "rekey.prev").read_text(encoding="utf-8"), encoding="utf-8")
    res = svc.rekey_server_answered()
    assert res["done"]["keys"] == "old" and res["done"]["ok"] is False and res["note"] == "", res
    assert host.txn() == {} and host.etc_files() == set()


def test_a_busy_transition_lock_skips_the_tick(svc, host, clock):
    """Идёт link_go или ответ сервера (замок перехода у другого потока) —
    такт не ждёт и ничего не решает: иначе он вернул бы прежние посреди
    переключения."""
    import threading
    _switched(svc, host, clock)
    clock.at(gwrekey.SWITCH_SECONDS + 5)
    held, release = threading.Event(), threading.Event()

    def holder():
        with gwrekey._RK_LOCK:
            held.set()
            release.wait(5)
    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        assert svc.rekey_tick() == QUIET
        assert host.txn()["state"] == "switched" and len(host.link_runs) == 1
    finally:
        release.set()
        t.join(5)
    assert svc.rekey_tick()["note"] == BACK_NOTE, "после освобождения замка срок не сработал"


def test_a_failed_search_step_waits_a_whole_interval(svc, host, clock):
    """Перестановка в поиске не встала (скрипт отказал) — следующая попытка не
    на ближайшем такте, а через шаг: линк не дёргают каждые 30 с."""
    _switched(svc, host, clock)
    host.alive = False
    clock.at(100)
    svc.rekey_tick()
    host.link_rc = 1
    tries = _tries(svc, host, clock, until=100 + gwrekey.SEARCH_FIRST * 3, step=30)
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    assert len(tries) >= 2 and all(g >= gwrekey.SEARCH_FIRST for g in gaps), f"попытки после отказа: {tries}"


def test_a_server_answer_without_a_transition_says_nothing(svc, host):
    assert svc.rekey_server_answered() == NONE_ANSWERED
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    assert svc.rekey_server_answered() == NONE_ANSWERED, "вопрос висит — закреплять нечего"
    assert host.txn()["state"] == "asked"
    svc.rekey_answer(True)
    assert svc.rekey_server_answered() == NONE_ANSWERED, "согласие без link_go закреплено как итог"
    assert host.txn()["state"] == "ready"


# ── клиент канала ────────────────────────────────────────────────────────────

async def test_link_next_is_acknowledged_and_asked_once(svc, host, monkeypatch):
    """link_next → link_ack asked и приглашение в чате; тот же link_next снова
    (новая сессия) → link_ack, но приглашения второй раз нет."""
    c = _Client(svc, monkeypatch)
    conf, _, _ = _next_conf(host)
    await c.client._handle(_msg(conf))
    assert c.sent == [("link_ack", {"id": "a1b2c3d4e5f6", "state": "asked", "error": ""})]
    assert c.asks == ["link"]
    await c.client._handle(_msg(conf))
    assert len(c.sent) == 2 and c.asks == ["link"], "приглашение задано второй раз"


async def test_a_replaced_invitation_is_edited_to_say_so(svc, host, monkeypatch):
    """Новое приглашение поверх висящего: прежнее сообщение правится —
    «Запрос заменён более новым», кнопки старого не ведут в никуда."""
    c = _Client(svc, monkeypatch)
    conf, _, _ = _next_conf(host)
    await c.client._handle(_msg(conf))
    svc.rekey_mark_msg(555, 42)
    newer, _, _ = _next_conf(host)
    await c.client._handle(_msg(newer, "dddd66667777"))
    assert c.edits == [(555, 42, "🔑 Новые ключи линка: пришёл более новый запрос — он ниже")] and c.notes == [], (c.edits, c.notes)
    assert c.asks == ["link", "link"]


async def test_the_answer_in_the_chat_reaches_the_server(svc, host, monkeypatch):
    """«Применить» в чате агента: link_ack ready уходит серверу сразу; частый
    такт не нужен — в ready сроков короче суток нет."""
    c = _Client(svc, monkeypatch)
    conf, _, _ = _next_conf(host)
    await c.client._handle(_msg(conf))
    reply = await linkclient.report_rekey_answer(svc, True)
    assert reply["state"] == "ready" and c.sent[-1] == ("link_ack", reply)


async def test_a_successful_link_go_closes_the_session_to_come_back_on_new_keys(svc, host, monkeypatch):
    """link_go встал: клиент сам закрывает сессию — TCP по линку пережил бы
    down/up, а фиксация на обеих сторонах ждёт новой сессии новым ключом;
    частый такт включён; серверу на старой сессии ничего не уходит."""
    c = _Client(svc, monkeypatch)
    _ready(svc, host)
    await c.client._handle({"t": "link_go", "id": "a1b2c3d4e5f6"})
    assert c.closed == 1 and c.kicks == 1, (c.closed, c.kicks)
    assert c.sent == [] and host.txn()["state"] == "switched"


async def test_a_failed_link_go_is_reported_as_declined_and_the_human_told(svc, host, monkeypatch):
    """link_go, который скрипт не поставил, — link_ack declined с причиной
    (сервер сразу вернёт прежние, а не через 3 минуты), сессия не рвётся,
    человеку — «вернул прежние» правкой приглашения."""
    c = _Client(svc, monkeypatch)
    _ready(svc, host)
    st = host.txn(); st["msg"] = [555, 42]
    (host.etc / "link.txn").write_text(json.dumps(st), encoding="utf-8")
    host.link_rc = 1
    await c.client._handle({"t": "link_go", "id": "a1b2c3d4e5f6"})
    kind, body = c.sent[-1]
    assert kind == "link_ack" and body["state"] == "declined" and body["id"] == "a1b2c3d4e5f6"
    assert "не прошёл проверку" in body["error"], body
    assert c.edits == [(555, 42, BACK_NOTE)] and c.closed == 0, (c.edits, c.closed)


async def test_link_cancel_tells_the_human_once(svc, host, monkeypatch):
    """Отзыв сервером — правка приглашения «Запрос отозван сервером AWG»;
    сообщения не знаем или правка не прошла — отдельное сообщение; второй
    отзыв того же id — тишина."""
    c = _Client(svc, monkeypatch)
    conf, _, _ = _next_conf(host)
    await c.client._handle(_msg(conf))
    await c.client._handle({"t": "link_cancel", "id": "a1b2c3d4e5f6"})
    await c.client._handle({"t": "link_cancel", "id": "a1b2c3d4e5f6"})
    assert c.notes == ["🔑 Новые ключи линка: запрос отозван сервером AWG"] and c.edits == [], (c.notes, c.edits)
    newer, _, _ = _next_conf(host)
    await c.client._handle(_msg(newer, "eeee88889999"))
    svc.rekey_mark_msg(555, 43)
    c.edit_ok = False
    await c.client._handle({"t": "link_cancel", "id": "eeee88889999"})
    assert c.edits == [(555, 43, "🔑 Новые ключи линка: запрос отозван сервером AWG")]
    assert c.notes == ["🔑 Новые ключи линка: запрос отозван сервером AWG"] * 2, "правка не прошла — человек не узнал"


async def test_the_first_server_word_after_the_switch_sends_link_done(svc, host, clock, monkeypatch):
    c = _Client(svc, monkeypatch)
    _switched(svc, host, clock)
    await c.client._rekey_answered()
    assert c.sent == [("link_done", {"id": "a1b2c3d4e5f6", "ok": True, "keys": "new"})]
    assert c.notes == ["✅ Линк на новых ключах"]


async def test_the_fast_tick_runs_while_the_transition_lives_and_stops_after(svc, host, monkeypatch):
    """Частый такт: тикает с периодом состояния, пока переход идёт, и сам
    заканчивается — вечная задача на каждом шлюзе ни к чему; второй запуск
    поверх идущего не создаёт второго цикла."""
    client = linkclient.LinkClient(svc)
    every = iter([0.001, 0.001, 0])
    monkeypatch.setattr(svc, "rekey_tick_every", lambda: next(every))
    ticks = []
    monkeypatch.setattr(svc, "rekey_tick", lambda: ticks.append(1) or QUIET)
    client.rekey_kick()
    task = client.__dict__["_rekey_task"]
    client.rekey_kick()
    assert client.__dict__["_rekey_task"] is task, "второй частый такт поверх идущего"
    await asyncio.wait_for(task, 2)
    assert ticks == [1, 1], ticks


async def test_the_monitor_tick_steps_aside_while_the_fast_tick_runs(svc, host, monkeypatch):
    """Такт монитора при живом цикле перехода пропускается — два такта в ту
    же секунду дали бы двойное «вернул прежние»; цикла нет — тикает сам и
    поднимает цикл."""
    client = linkclient.LinkClient(svc)
    ticks = []
    monkeypatch.setattr(svc, "rekey_tick", lambda: ticks.append(1) or QUIET)
    monkeypatch.setattr(svc, "rekey_tick_every", lambda: 3600)
    client.rekey_kick()
    await asyncio.sleep(0)
    await client.rekey_tick_monitor()
    assert ticks == [], "монитор тикнул поверх цикла перехода"
    client.rekey_stop()
    await asyncio.sleep(0)
    assert "_rekey_task" not in client.__dict__
    await client.rekey_tick_monitor()
    assert ticks == [1] and not client.__dict__["_rekey_task"].done(), "монитор не поднял цикл"
    client.rekey_stop()


async def test_a_stale_link_go_answers_declined_without_bothering_the_human(svc, host, monkeypatch):
    """link_go без согласия или с чужим id (устаревший): серверу declined,
    человеку — ничего: ничего не трогали."""
    c = _Client(svc, monkeypatch)
    await c.client._handle({"t": "link_go", "id": "a1b2c3d4e5f6"})
    assert c.sent and c.sent[-1][1]["state"] == "declined", c.sent
    assert c.notes == [] and c.edits == [], (c.notes, c.edits)


async def test_the_uplink_tick_reports_the_answer_and_tells_the_human(svc, monkeypatch):
    """Такт аплинка клиента: ответ аварийного применения — серверу тем же
    путём, что ответ человека (очередь ответов), заметки — в чат по одной."""
    client = linkclient.LinkClient(svc)
    answer = {"ok": True, "section": "uplink", "accepted": True, "gen": 3, "hash": "ab", "print": "p"}
    monkeypatch.setattr(svc, "cfg_uplink_tick", lambda: {"answer": answer, "notes": ["раз", "два"]})
    reported, notes = [], []

    async def report(services, ans):
        reported.append(ans)

    async def notify(text):
        notes.append(text)
    monkeypatch.setattr(linkclient, "report_cfg_answer", report)
    monkeypatch.setattr(linkclient, "_notify", notify)
    await client.uplink_tick()
    assert reported == [answer] and notes == ["раз", "два"]
    monkeypatch.setattr(svc, "cfg_uplink_tick", lambda: {"answer": None, "notes": []})
    await client.uplink_tick()
    assert reported == [answer] and notes == ["раз", "два"], "пустой такт что-то отправил"
