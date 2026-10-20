"""Голосование в планировщике резерва (runtime/scheduler/reserve.py): такт
живости линка пары и такт голосования — одно задание в 5 с, поэтому строка
Н4 решается в том же такте, что и «основа недоступна» (t0).

Планировщик — журнал add_job, задание вызывается напрямую; канал пары —
журнал pairlink.send; бот — FakeBot, исполнитель действий — с ним же.

Цена ошибки: голосование отдельным, более редким заданием — Т-РУ7б и приём
кода позже t0 на его период, админ основы ночью ждёт без причины; такт,
шлющий что-либо в простое, — маячок.
"""
from __future__ import annotations

import pytest

from awgbot.runtime import pairlink, rsvactions
from awgbot.runtime.scheduler import reserve as jobs
from tests.conftest import FakeBot
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import ADMIN, PEER_ADMIN, box

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]


class _Sched:
    def __init__(self):
        self.jobs: dict[str, tuple] = {}

    def add_job(self, fn, trigger, id, **kw):
        self.jobs[id] = (fn, trigger, kw)


@pytest.fixture()
def wired(box, monkeypatch):
    sent: list[tuple] = []

    async def send(kind, body=None):
        sent.append((kind, dict(body or {})))
        return True
    monkeypatch.setattr(pairlink, "send", send)
    bot = FakeBot()
    monkeypatch.setattr(rsvactions, "_ctx", {"bot": bot, "services": None})
    sched = _Sched()
    jobs.setup_reserve_jobs(sched, box.s, bot)
    real = box.s.reserve_liveness_tick
    monkeypatch.setattr(box.s, "reserve_liveness_tick", lambda now=None: real(now=box.clock.t))
    monkeypatch.setattr(box.s, "_pair_rx", lambda: 500)
    sched.sent, sched.bot = sent, bot
    return sched


async def test_h4_is_told_in_the_same_job_run_as_the_primary_going_down(box, wired):
    box.peer_admin_opened()
    fn = wired.jobs["rsv_liveness"][0]
    await fn()                                          # первый замер — жизнь
    for _ in range(11):                                 # 55 с без keepalive линка пары
        box.advance(5)
        await fn()
        if box.s.reserve_peer_seen() == "down":
            break
    assert box.s.reserve_peer_seen() == "down"
    msgs = [r for r in wired.bot.records if r[0] == "send_message"]
    to_peer = [r for r in msgs if r[1] == PEER_ADMIN]
    to_admin = [r for r in msgs if r[1] == ADMIN]
    assert len(to_peer) == 1 and "аварийный код" in to_peer[0][2], f"Т-РУ7б не в такте t0: {msgs}"
    assert any("аварийный код" in r[2] for r in to_admin), "Т-РУ7 не в такте t0"
    before = len(wired.bot.records)
    for _ in range(6):
        box.advance(5)
        await fn()
    assert len(wired.bot.records) == before, "Н4 повторила уведомления"


async def test_the_job_is_silent_while_everything_is_fine(box, wired, monkeypatch):
    rx = {"v": 500}
    monkeypatch.setattr(box.s, "_pair_rx", lambda: rx["v"])
    box.hello(1, sees=True)
    fn = wired.jobs["rsv_liveness"][0]
    wired.sent.clear()
    for i in range(240):                                # 20 минут
        box.advance(5)
        if i % 6 == 0:
            rx["v"] += 148
            box.keepalive()
        await fn()
    assert not wired.sent and not wired.bot.records, f"в простое такт говорил: {wired.sent[:3]} {wired.bot.records[:3]}"
