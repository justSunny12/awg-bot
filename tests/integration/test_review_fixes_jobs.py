"""Задание живости резерва: сброс отметки «конфиг зонда отправлен» (qprobe
отброшен без канала пары) — запись в базу, она идёт в потоке, а не в цикле
событий, как соседние вызовы сервисов такта.

Цена ошибки: запись под замком базы в цикле событий — на время fsync или
ожидания замка замирают поллинг Telegram, каналы шлюзов и пары, ответчики.
"""
from __future__ import annotations

import threading
import types

import pytest

pytestmark = pytest.mark.integration


class _Sched:
    def __init__(self):
        self.jobs: dict[str, tuple] = {}

    def add_job(self, fn, trigger, id, **kw):
        self.jobs[id] = (fn, trigger, kw)


async def test_qprobe_reset_runs_off_the_event_loop(monkeypatch):
    from awgbot.runtime import pairlink, quorum as quorum_rt, rsvactions
    from awgbot.runtime.scheduler import reserve as jobs
    threads: list[threading.Thread] = []
    svc = types.SimpleNamespace(reserve_liveness_tick=lambda: ("", []), reserve_gwguest_tick=lambda: [],
                                reserve_st_if_changed=lambda: None,
                                reserve_qprobe_reset=lambda: threads.append(threading.current_thread()))

    async def run(acts):
        return None

    async def qtick(services, act):
        await act([("send", "qprobe", {"l": "a", "g": 1, "conf": "DUMMY"})])
    monkeypatch.setattr(pairlink, "online", lambda: False)
    monkeypatch.setattr(rsvactions, "run", run)
    monkeypatch.setattr(quorum_rt, "tick", qtick)
    sched = _Sched()
    jobs.setup_reserve_jobs(sched, svc, None)
    await sched.jobs["rsv_liveness"][0]()
    assert len(threads) == 1, "сброс отметки qprobe не вызван"
    assert threads[0] is not threading.current_thread(), "сброс отметки qprobe выполнен в цикле событий"
