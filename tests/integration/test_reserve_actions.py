"""Исполнитель действий автоматов (runtime/rsvactions.py), разбор сообщений
переезда и возврата сессией канала (runtime/pairlink/inbound.py) и такты
планировщика (runtime/scheduler/reserve.py).

Автоматы возвращают списки действий; здесь проверяется, что действия
выполняются в нужном порядке и что сбой одного не останавливает остальные.

Цена ошибки: `release`, отдающий реплику до остановки поллинга и опроса вне
очереди, — пользователи, подключившиеся между репликой и остановкой, пропали у
резерва; реплика, не ушедшая, а поллинг не возвращённый, — бот молчит у обоих
серверов; сбой первого действия, останавливающий остальные, — уведомление
админу не уходит из-за разрыва канала; реплика частями, склеенная по первой
пришедшей части, — слияние на обрывке.
"""
from __future__ import annotations

import types

import pytest

from awgbot.runtime import pairlink, rsvactions
from awgbot.runtime.pairlink import guestctl
from awgbot.runtime.pairlink.inbound import InboundMixin
from awgbot.runtime.scheduler import reserve as jobs
from awgbot.util import pairproto
from tests.conftest import FakeBot

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_jobs import _Sched

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


class _Pc:
    """Управление поллингом: журнал вызовов в общем списке событий."""

    def __init__(self, log, *, release_ok=True, take_ok=True):
        self.log, self.release_ok, self.take_ok = log, release_ok, take_ok

    async def release(self):
        self.log.append("pc.release")
        return self.release_ok

    async def take(self):
        self.log.append("pc.take")
        return self.take_ok

    def wake(self):
        self.log.append("pc.wake")


@pytest.fixture()
def act(mv, monkeypatch):
    log: list = []
    sent: list = []
    released: list = []
    box = types.SimpleNamespace(log=log, sent=sent, released=released, send_ok=True, released_ok=True)

    async def send(kind, body=None):
        sent.append((kind, dict(body or {})))
        return box.send_ok

    async def send_released(built):
        log.append("send_released")
        released.append(built)
        return box.released_ok
    monkeypatch.setattr(pairlink, "send", send)
    monkeypatch.setattr(pairlink, "send_released", send_released)
    svc = mv.svc
    real_poll = getattr(svc, "poll_traffic", None)

    def poll():
        log.append("poll_traffic")
        if box.__dict__.get("poll_error"):
            raise RuntimeError("опрос упал")
    monkeypatch.setattr(svc, "poll_traffic", poll, raising=False)
    bot = FakeBot()
    box.pc = _Pc(log)
    rsvactions.setup(bot, svc, box.pc)
    box.mv, box.svc, box.bot, box.real_poll = mv, svc, bot, real_poll
    yield box
    rsvactions._ctx.clear()


# ── release ─────────────────────────────────────────────────────────────────

async def test_release_stops_the_polling_then_polls_traffic_then_sends_the_replica(mv, act):
    rm.to_step(act.svc, "release", gen=9)
    await rsvactions.run([("release",)])
    assert act.log == ["pc.release", "poll_traffic", "send_released"], act.log
    assert act.released[0]["gen"] == 9 and act.released[0]["blob"]
    assert act.svc.rsv_get("rsv_out")["step"] == "released" and act.svc.reserve_poll() == "away"


async def test_release_goes_on_when_the_off_queue_poll_fails(mv, act):
    rm.to_step(act.svc, "release")
    act.poll_error = True
    await rsvactions.run([("release",)])
    assert act.released, "опрос вне очереди упал — реплика всё равно должна уйти, иначе переезд зависнет"


async def test_release_outside_its_step_sends_nothing_and_gives_the_polling_back(mv, act):
    await rsvactions.run([("release",)])
    assert not act.released and act.log[-1] == "pc.wake", act.log


async def test_a_replica_that_did_not_leave_returns_the_right_to_answer_to_the_primary(mv, act):
    rm.to_step(act.svc, "release")
    act.released_ok = False
    await rsvactions.run([("release",)])
    assert act.svc.rsv_get("rsv_out")["step"] == "release", "шаг должен вернуться: реплика не ушла"
    assert act.svc.reserve_poll() == "mine" and act.log[-1] == "pc.wake", \
        "реплика не ушла, а поллинг не возвращён — бот молчит у обоих серверов"


# ── take_tg и прочие действия ───────────────────────────────────────────────

async def test_take_tg_confirms_with_taken_and_moves_the_record_step(mv, act):
    rm.go_away(act.svc)
    act.svc.reserve_set_out("returning", kind="button", gen=7)
    act.svc._out_step("take_tg", 300)
    await rsvactions.run([("take_tg",)])
    assert ("taken", {}) in act.sent and act.svc.reserve_poll() == "mine"
    assert act.svc.rsv_get("rsv_out")["step"] == "record_back"


async def test_a_failed_take_tg_confirms_nothing(mv, act):
    act.pc.take_ok = False
    act.svc.reserve_set_out("returning", kind="button", gen=7)
    act.svc._out_step("take_tg", 300)
    await rsvactions.run([("take_tg",)])
    assert act.sent == [] and act.svc.rsv_get("rsv_out")["step"] == "take_tg", "deleteWebhook не прошёл, а возврат пошёл дальше"


async def test_a_failing_action_does_not_stop_the_rest(mv, act, monkeypatch):
    async def boom(kind, body=None):
        raise RuntimeError("канал оборван")
    monkeypatch.setattr(pairlink, "send", boom)
    from awgbot.domain.services.types import Notification
    await rsvactions.run([("send", "taken", {}), ("notify", [Notification(tg_id=rc.ADMIN, text="итог")])])
    assert [r for r in act.bot.records if r[0] == "send_message"] == [("send_message", rc.ADMIN, "итог")]


async def test_an_unknown_action_is_skipped(mv, act):
    await rsvactions.run([("странное", 1), None, ()])


async def test_fw_and_restart_actions_call_the_host_layer(mv, act, monkeypatch):
    calls = []
    monkeypatch.setattr(act.svc, "reconcile_ssh_access", lambda *a, **k: calls.append("fw"), raising=False)
    monkeypatch.setattr(act.svc, "restart_bot", lambda *a, **k: calls.append("restart"), raising=False)
    await rsvactions.run([("fw",), ("restart",)])
    assert calls == ["fw", "restart"]


async def test_the_guest_action_sends_through_the_local_channel_and_survives_its_absence(mv, act, monkeypatch):
    got = []

    class Srv:
        async def send(self, kind, body=None):
            got.append((kind, body))
            return True
    monkeypatch.setattr(guestctl, "server", lambda: Srv())
    await rsvactions.run([("guest", "stop", {})])
    assert got == [("stop", {})]
    monkeypatch.setattr(guestctl, "server", lambda: None)
    await rsvactions.run([("guest", "stop", {})])


# ── сессия канала: released частями, ветвление по состоянию ─────────────────

class _Handler(InboundMixin):
    def __init__(self, services):
        self.services = services


def _sess():
    return types.SimpleNamespace(parts={}, listener=True)


async def test_the_replica_parts_are_glued_once_in_any_order_and_routed_to_the_merge_at_the_primary(mv, act, monkeypatch):
    svc = act.svc
    got = {"merge": [], "store": []}
    monkeypatch.setattr(svc, "reserve_released_in", lambda blob: got["merge"].append(blob) or [])
    monkeypatch.setattr(svc, "reserve_released_store", lambda blob: got["store"].append(blob) or [])
    rm.go_away(svc)
    svc.reserve_set_out("returning", gen=7)
    h, s = _Handler(svc), _sess()
    body = lambda i, blob: {"gen": 7, "i": i, "n": 3, "blob": blob}                # noqa: E731
    await h._in_released(s, body(2, "cc"))
    await h._in_released(s, body(0, "aa"))
    await h._in_released(s, body(0, "aa"))                                          # повтор части
    assert got == {"merge": [], "store": []}, "слияние началось на обрывке"
    await h._in_released(s, body(1, "bb"))
    assert got["merge"] == [bytes.fromhex("aabbcc")] and got["store"] == []
    assert s.parts == {}, "склеенная реплика осталась в памяти сессии"


async def test_the_replica_goes_to_the_guest_storage_at_the_reserve(mv, act, monkeypatch):
    svc = act.svc
    got = {"merge": [], "store": []}
    monkeypatch.setattr(svc, "reserve_released_in", lambda blob: got["merge"].append(blob) or [])
    monkeypatch.setattr(svc, "reserve_released_store", lambda blob: got["store"].append(blob) or [])
    await _Handler(svc)._in_released(_sess(), {"gen": 7, "i": 0, "n": 1, "blob": "0102"})
    assert got == {"merge": [], "store": [b"\x01\x02"]}


async def test_a_part_index_outside_the_count_is_a_protocol_error(mv, act):
    with pytest.raises(pairproto.ProtocolError):
        await _Handler(act.svc)._in_released(_sess(), {"gen": 7, "i": 3, "n": 3, "blob": "00"})
    with pytest.raises(pairproto.ProtocolError):
        await _Handler(act.svc)._in_released(_sess(), {"gen": 7, "i": 0, "n": 0, "blob": "00"})


async def test_a_replica_that_is_not_hex_is_dropped_without_a_merge(mv, act, monkeypatch):
    svc = act.svc
    calls = []
    monkeypatch.setattr(svc, "reserve_released_in", lambda blob: calls.append(blob) or [])
    monkeypatch.setattr(svc, "reserve_released_store", lambda blob: calls.append(blob) or [])
    rm.go_away(svc)
    svc.reserve_set_out("returning", gen=7)
    await _Handler(svc)._in_released(_sess(), {"gen": 7, "i": 0, "n": 1, "blob": "не hex"})
    assert calls == []


async def test_go_with_a_refusal_answers_down_and_without_one_stays_silent(mv, act, monkeypatch):
    sess_sent = []

    async def send_line(sess, kind, body=None):
        sess_sent.append((kind, body))
    from awgbot.runtime.pairlink import inbound
    monkeypatch.setattr(inbound, "send_line", send_line)
    svc = act.svc
    h = _Handler(svc)
    await h._in_go_req(_sess(), {"gen": 99, "kind": "button"})
    assert sess_sent and sess_sent[0][0] == "down" and "99" in sess_sent[0][1]["reason"], "отказ go_req — down по сессии"
    sess_sent.clear()
    # отказ конверту — действием автомата (при ждущем процессе это срыв приёма целиком)
    await h._in_go(_sess(), {"gen": 7, "kind": "button", "sealed": "ab" * 32, "oid": "0123456789abcdef"})
    assert sess_sent == [], "down на go ушёл мимо действий автомата"
    assert len(act.sent) == 1 and act.sent[0][0] == "down" and "не ждёт" in act.sent[0][1]["reason"], act.sent
    act.sent.clear()
    monkeypatch.setattr(svc, "reserve_go_in", lambda *a, **kw: "")
    await h._in_go(_sess(), {"gen": 7, "kind": "button", "sealed": "ab" * 32, "oid": "0123456789abcdef"})
    assert sess_sent == [] and act.sent == [], "без причины отказа down уходить не должен"


async def test_a_refused_envelope_while_the_process_waits_fails_the_hosting_through_the_actions(mv, act, monkeypatch):
    """Отказ конверту при ждущем процессе — `_take_fail`: его действия (down,
    fw, уведомление) исполняются как действия автомата."""
    svc = act.svc
    monkeypatch.setattr(svc, "reserve_go_in", lambda *a, **kw: "ключ не к этому открытию")
    got = []
    monkeypatch.setattr(svc, "reserve_go_refused",
                        lambda why, sealed: got.append((why, sealed)) or [("send", "down", {"reason": why}), ("fw",)])
    await _Handler(svc)._in_go(_sess(), {"gen": 7, "kind": "button", "sealed": "ab" * 32, "oid": "0123456789abcdef"})
    assert got == [("ключ не к этому открытию", True)]
    assert act.sent == [("down", {"reason": "ключ не к этому открытию"})], act.sent
    got.clear()
    await _Handler(svc)._in_go(_sess(), {"gen": 7, "kind": "button", "k": "ab" * 32})
    assert got == [("ключ не к этому открытию", False)], "go старого вида без конверта помечен как конверт"


async def test_open_eph_from_the_reserve_reaches_the_service_with_the_key_and_the_opening_id(mv, act, monkeypatch):
    """`open_eph` — разовый ключ процесса открытия резерва: его действия
    (`go{sealed}`) исполняются как действия автомата."""
    svc = act.svc
    got = []
    go = ("send", "go", {"gen": 7, "kind": "button", "ttl": 0, "sealed": "ef" * 40, "oid": "0123456789abcdef"})
    monkeypatch.setattr(svc, "reserve_open_eph_in", lambda gen, eph, oid: got.append((gen, eph, oid)) or [go])
    await _Handler(svc)._in_open_eph(_sess(), {"gen": 7, "eph": "cd" * 32, "oid": "0123456789abcdef"})
    assert got == [(7, "cd" * 32, "0123456789abcdef")]
    assert act.sent == [("go", go[2])], f"go с конвертом не ушёл в канал: {act.sent}"


async def test_blocks_from_the_primary_go_to_the_guest_and_are_capped(mv, act, monkeypatch):
    got = []

    class Srv:
        async def send(self, kind, body=None):
            got.append((kind, body))
            return True
    monkeypatch.setattr(guestctl, "server", lambda: Srv())
    # блокировки основы гостю — только пока я несу её пользователей (иначе `blocks` — от
    # держателя мне самой, следующей)
    act.svc.reserve_set_in("serving")
    await _Handler(act.svc)._in_blocks(_sess(), {"addrs": ["10.8.1.2"] * 5000})
    assert got and got[0][0] == "blocks" and len(got[0][1]["addrs"]) == 4096, got


# ── такты планировщика ──────────────────────────────────────────────────────

@pytest.fixture()
def wired(mv, act, monkeypatch):
    sched = _Sched()
    ran: list = []

    async def run(actions):
        ran.append(list(actions))
    monkeypatch.setattr(rsvactions, "run", run)
    jobs.setup_reserve_jobs(sched, mv.svc, act.bot)
    sched.ran, sched.svc = ran, mv.svc
    return sched


async def test_the_move_tick_runs_out_then_in_and_hands_both_lists_to_the_executor(mv, wired, monkeypatch):
    svc = wired.svc
    monkeypatch.setattr(svc, "reserve_out_tick", lambda: [("a",)])
    monkeypatch.setattr(svc, "reserve_in_tick", lambda: [("b",)])
    monkeypatch.setattr(guestctl, "server", lambda: None)
    await wired.jobs["rsv_move_tick"][0]()
    assert wired.ran == [[("a",)], [("b",)]]


async def test_the_guest_words_become_the_matching_service_calls(mv, wired, monkeypatch):
    svc = wired.svc
    calls = []
    monkeypatch.setattr(svc, "reserve_out_tick", lambda: [])
    monkeypatch.setattr(svc, "reserve_in_tick", lambda: [])
    monkeypatch.setattr(svc, "reserve_guest_tg_taken", lambda: calls.append("tg_taken") or [("x",)])
    monkeypatch.setattr(svc, "reserve_guest_replica_in", lambda blob: calls.append(("replica", blob)) or [("y",)])
    monkeypatch.setattr(svc, "reserve_guest_back_req", lambda: calls.append("back_req") or [("z",)])
    srv = types.SimpleNamespace(take_words=lambda: [("tg_taken", None), ("replica", b"\x01"), ("back_req", None)])
    monkeypatch.setattr(guestctl, "server", lambda: srv)
    await wired.jobs["rsv_move_tick"][0]()
    assert calls == ["tg_taken", ("replica", b"\x01"), "back_req"]
    assert [("x",)] in wired.ran and [("y",)] in wired.ran and [("z",)] in wired.ran



async def test_the_webhook_word_of_the_guest_reaches_the_reserve_admin_and_the_online_count_is_kept(mv, wired,
                                                                                                   monkeypatch):
    """Слово гостя о надзоре вебхука — в Т-РУ19 админу резерва; число
    устройств основы у гостя — в st основе («там M»), без связи — 0."""
    svc = wired.svc
    monkeypatch.setattr(svc, "reserve_out_tick", lambda: [])
    monkeypatch.setattr(svc, "reserve_in_tick", lambda: [])
    got = []
    monkeypatch.setattr(svc, "reserve_guest_webhook_in", lambda body: got.append(body) or [("w",)])
    srv = types.SimpleNamespace(take_words=lambda: [("webhook", {"stalled": True, "since": "", "error": "x"})],
                                online_count=9, connected=lambda: True)
    monkeypatch.setattr(guestctl, "server", lambda: srv)
    await wired.jobs["rsv_move_tick"][0]()
    assert got == [{"stalled": True, "since": "", "error": "x"}] and [("w",)] in wired.ran
    assert svc.reserve_st_body()["online"] == 9, "число устройств у гостя не уходит основе"
    srv.connected = lambda: False
    srv.take_words = lambda: []
    await wired.jobs["rsv_move_tick"][0]()
    assert svc.reserve_st_body()["online"] == 0, "гость отключился, а основе по-прежнему «там 9»"

async def test_a_failing_automaton_does_not_stop_the_guest_words_and_the_blocks(mv, wired, monkeypatch):
    svc = wired.svc

    def boom():
        raise RuntimeError("автомат упал")
    monkeypatch.setattr(svc, "reserve_out_tick", boom)
    calls = []
    monkeypatch.setattr(svc, "reserve_guest_tg_taken", lambda: calls.append("tg_taken") or [])
    srv = types.SimpleNamespace(take_words=lambda: [("tg_taken", None)])
    monkeypatch.setattr(guestctl, "server", lambda: srv)
    monkeypatch.setattr(svc, "reserve_blocks_to_send", lambda: ["10.8.1.2"])
    await wired.jobs["rsv_move_tick"][0]()
    assert calls == ["tg_taken"], "слово гостя потеряно из-за сбоя автомата"
    assert [("send", "blocks", {"addrs": ["10.8.1.2"]})] in wired.ran


async def test_blocks_are_sent_only_when_the_service_has_a_new_set(mv, wired, monkeypatch):
    svc = wired.svc
    monkeypatch.setattr(svc, "reserve_out_tick", lambda: [])
    monkeypatch.setattr(svc, "reserve_in_tick", lambda: [])
    monkeypatch.setattr(guestctl, "server", lambda: None)
    monkeypatch.setattr(svc, "reserve_blocks_to_send", lambda: None)
    await wired.jobs["rsv_move_tick"][0]()
    assert not [r for r in wired.ran if r and r[0][0] == "send"]


async def test_the_slow_tick_runs_the_watch_the_return_notice_and_the_clone_count_even_if_one_fails(mv, wired, monkeypatch):
    svc = wired.svc
    from awgbot.domain.services.types import Notification
    calls = []

    def boom():
        calls.append("watch")
        raise RuntimeError("DNS упал")
    monkeypatch.setattr(svc, "reserve_record_watch_tick", boom)
    monkeypatch.setattr(svc, "reserve_return_notify_tick",
                        lambda: calls.append("return") or [Notification(tg_id=1, text="вернулся")])
    monkeypatch.setattr(svc, "reserve_traffic_in_tick", lambda: calls.append("traffic"))
    monkeypatch.setattr(svc, "reserve_tg_learn_tick", lambda: calls.append("tg_learn") or [])
    monkeypatch.setattr(svc, "reserve_tg_pool_tick", lambda: calls.append("tg_pool") or [])
    await wired.jobs["rsv_slow_tick"][0]()
    assert calls == ["watch", "return", "traffic", "tg_learn", "tg_pool"], "шаг медленного такта пропал после сбоя соседа"
    ran = [r for r in wired.ran if r]
    assert len(ran) == 1 and ran[0][0][0] == "notify", f"кроме уведомления о возврате ушло что-то ещё: {wired.ran}"


# ── реплика частями ─────────────────────────────────────────────────────────

async def test_send_released_cuts_the_replica_into_numbered_parts_and_sends_an_empty_one_whole(monkeypatch):
    got: list[tuple] = []

    async def send(kind, body=None):
        got.append((kind, dict(body)))
        return True
    monkeypatch.setattr(pairlink, "send", send)
    blob = "ab" * (pairproto.CHUNK_BYTES * 2 + 5)
    assert await pairlink.send_released({"gen": 7, "blob": blob}) is True
    assert [(k, b["gen"], b["i"], b["n"]) for k, b in got] == [("released", 7, 0, 3), ("released", 7, 1, 3), ("released", 7, 2, 3)]
    assert "".join(b["blob"] for _k, b in got) == blob
    got.clear()
    assert await pairlink.send_released({"gen": 7, "blob": ""}) is True
    assert got == [("released", {"gen": 7, "i": 0, "n": 1, "blob": ""})]


async def test_send_released_stops_at_the_first_part_that_did_not_go(monkeypatch):
    calls = []

    async def send(kind, body=None):
        calls.append(body["i"])
        return body["i"] < 1
    monkeypatch.setattr(pairlink, "send", send)
    assert await pairlink.send_released({"gen": 7, "blob": "ab" * (pairproto.CHUNK_BYTES * 2 + 5)}) is False
    assert calls == [0, 1], "после оборвавшейся части продолжаем слать — на том конце обрывок"
