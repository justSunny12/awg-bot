"""Задания резерва в планировщике (runtime/scheduler/reserve.py): живость
линка пары раз в 5 с и срок приглашения раз в минуту.

Планировщик подменён журналом add_job: задания вызываются напрямую тем же
телом, что их вызвал бы APScheduler. Канал пары — журнал pairlink.send /
pair_close / ensure.

Цена ошибки: такт живости, который шлёт `ping` без подозрения, — маячок по
ровному такту; проба, ушедшая без нонса такта, — ответ не снимет
подозрение; час прошёл, а линк под приглашение жив и слушатель на месте —
приглашение действует дольше обещанного.
"""
from __future__ import annotations

import pytest

from awgbot.runtime import pairlink
from awgbot.runtime.scheduler import reserve as jobs
from awgbot.util import timeutil
from tests.conftest import FakeBot
from tests.integration import _reserve_common as rc

pytestmark = pytest.mark.integration
rsv = rc.rsv


class _Sched:
    def __init__(self):
        self.jobs: dict[str, tuple] = {}

    def add_job(self, fn, trigger, id, **kw):
        self.jobs[id] = (fn, trigger, kw)


@pytest.fixture()
def wired(services, rsv, monkeypatch):
    calls: list[tuple] = []

    async def send(kind, body=None):
        calls.append(("send", kind, dict(body or {})))
        return True

    async def pair_close(why, notify=False):
        calls.append(("pair_close", why, notify))

    async def ensure(svc):
        calls.append(("ensure",))
    monkeypatch.setattr(pairlink, "send", send)
    monkeypatch.setattr(pairlink, "pair_close", pair_close)
    monkeypatch.setattr(pairlink, "ensure", ensure)
    sched = _Sched()
    bot = FakeBot()
    jobs.setup_reserve_jobs(sched, services, bot)
    sched.calls, sched.bot = calls, bot
    return sched


def test_the_jobs_are_registered_with_their_periods(wired):
    assert set(wired.jobs) == {"rsv_liveness", "rsv_invite_tick", "rsv_pkg_tick", "rsv_move_tick", "rsv_slow_tick"}
    assert wired.jobs["rsv_liveness"][1].interval.total_seconds() == 5
    assert wired.jobs["rsv_invite_tick"][1].interval.total_seconds() == 60
    assert wired.jobs["rsv_pkg_tick"][1].interval.total_seconds() == 5
    assert wired.jobs["rsv_move_tick"][1].interval.total_seconds() == 3
    assert wired.jobs["rsv_slow_tick"][1].interval.total_seconds() == 60
    assert wired.jobs["rsv_slow_tick"][1].jitter == 15, "строго периодический запрос выглядит маячком"
    assert all(kw.get("max_instances") == 1 and kw.get("coalesce") for _f, _t, kw in wired.jobs.values())


async def test_the_liveness_job_probes_only_on_suspicion_with_the_ticks_nonce(services, rsv, wired, monkeypatch):
    rc.pair_row(services)
    real = services.reserve_liveness_tick
    clock = {"t": 1000.0}
    monkeypatch.setattr(services, "_pair_rx", lambda: 100)
    monkeypatch.setattr(services, "reserve_liveness_tick", lambda now=None: real(now=clock["t"]))
    fn = wired.jobs["rsv_liveness"][0]
    for t in (1000, 1005, 1020, 1035):
        clock["t"] = t
        await fn()
    assert not wired.calls, f"проба без подозрения: {wired.calls}"
    clock["t"] = 1040
    await fn()
    assert wired.calls == [("send", "ping", {"n": services.reserve_ping_nonce()})], wired.calls


async def test_the_liveness_job_sends_the_down_notice_to_the_admin(services, rsv, wired, monkeypatch):
    rc.pair_row(services)
    real = services.reserve_liveness_tick
    clock = {"t": 1000.0}
    monkeypatch.setattr(services, "_pair_rx", lambda: 100)
    monkeypatch.setattr(services, "reserve_liveness_tick", lambda now=None: real(now=clock["t"]))
    fn = wired.jobs["rsv_liveness"][0]
    for t in range(1000, 1100, 5):
        clock["t"] = t
        await fn()
    texts = [r[2] for r in wired.bot.records if r[0] == "send_message"]
    assert len([t for t in texts if "Нет связи с резервом" in t]) == 1, texts


async def test_the_invite_job_closes_an_expired_invitation_and_keeps_the_channel_in_step(services, rsv, wired,
                                                                                       monkeypatch):
    services.reserve_invite_issue()
    fn = wired.jobs["rsv_invite_tick"][0]
    await fn()
    assert wired.calls == [("ensure",)], "свежее приглашение закрыто тактом"
    later = timeutil.shift(timeutil.now(), 3601)
    monkeypatch.setattr(timeutil, "now", lambda: later)
    wired.calls.clear()
    await fn()
    assert wired.calls[0] == ("pair_close", "expired", True) and wired.calls[-1] == ("ensure",), wired.calls
    assert services.reserve_invite() == {} and rsv.modes[-1] == "--rollback"


async def test_a_failing_tick_does_not_break_the_job(services, rsv, wired, monkeypatch):
    def boom(now=None):
        raise RuntimeError("awg show упал")
    monkeypatch.setattr(services, "reserve_liveness_tick", boom)
    await wired.jobs["rsv_liveness"][0]()                 # не бросает: задание живёт дальше
