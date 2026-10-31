"""Живость линка пары и сессия канала (runtime/scheduler/reserve.py `job_liveness`,
runtime/pairlink `drop_session`/`wake`): «недоступен» по счётчику линка
закрывает текущую TCP-сессию канала у обеих сторон (слушателя и клиента),
«снова на связи» будит клиента — не ждать отступа.

Цена ошибки: полуоткрытая сессия живёт до keepalive ядра (минуты): слова
второй стороне уходят в никуда и считаются отправленными, а после возврата
линка клиент досиживает отступ, хотя линк уже жив.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from awgbot.runtime import pairlink
from awgbot.runtime.pairlink import client as pair_client
from awgbot.runtime.pairlink import common as pair_common
from awgbot.runtime.pairlink import server as pair_server
from awgbot.runtime.scheduler import reserve as sched

pytestmark = pytest.mark.unit


class _Writer:
    def __init__(self):
        self.closed = False
        self.transport = self

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def abort(self):
        self.closed = True

    def is_closing(self):
        return self.closed


class _Sched:
    def __init__(self):
        self.jobs = {}

    def add_job(self, fn, trigger, *, id, **kw):
        self.jobs[id] = fn


class _Svc:
    def __init__(self, act: str):
        self.act = act

    def reserve_liveness_tick(self):
        return self.act, []

    def reserve_ping_nonce(self):
        return "n"


def _job(monkeypatch, act: str):
    s = _Sched()
    sched.setup_reserve_jobs(s, _Svc(act), bot=None)
    # остальное тело такта — не об этом: канала нет, кворум и голосование молчат
    monkeypatch.setattr(pairlink, "online", lambda: False)

    async def _noop(*a, **k):
        return []
    from awgbot.runtime import quorum, rsvactions
    monkeypatch.setattr(quorum, "tick", _noop)
    monkeypatch.setattr(rsvactions, "run", _noop)
    return s.jobs["rsv_liveness"]


async def test_down_from_the_liveness_tick_drops_the_half_open_session(monkeypatch):
    seen = []

    async def drop():
        seen.append("drop")
    monkeypatch.setattr(pairlink, "drop_session", drop)
    monkeypatch.setattr(pairlink, "wake", lambda: seen.append("wake"))
    await _job(monkeypatch, "down")()
    assert seen == ["drop"], f"по «down» живости сессия не закрыта: {seen}"


async def test_up_from_the_liveness_tick_wakes_the_client(monkeypatch):
    seen = []

    async def drop():
        seen.append("drop")
    monkeypatch.setattr(pairlink, "drop_session", drop)
    monkeypatch.setattr(pairlink, "wake", lambda: seen.append("wake"))
    await _job(monkeypatch, "up")()
    assert seen == ["wake"], f"по «up» живости клиент не разбужен: {seen}"


async def test_a_quiet_tick_touches_nothing(monkeypatch):
    seen = []

    async def drop():
        seen.append("drop")
    monkeypatch.setattr(pairlink, "drop_session", drop)
    monkeypatch.setattr(pairlink, "wake", lambda: seen.append("wake"))
    await _job(monkeypatch, "")()
    assert seen == []


async def test_the_listener_closes_its_session_but_keeps_listening(monkeypatch):
    srv = pair_server.PairServer(object())
    w = _Writer()
    srv._sess = pair_common._Session(w, b"k" * 32, listener=True)
    srv._sess.hello = True
    srv._server = object()                                   # слушатель поднят
    monkeypatch.setattr(pairlink, "_link", srv)
    await pairlink.drop_session()
    assert w.closed, "сессия слушателя не закрыта"
    assert srv._server is not None, "по «down» снят сам слушатель — новой сессии принять некому"


async def test_the_client_closes_its_session_and_keeps_its_loop(monkeypatch):
    c = pair_client.PairClient(object())
    w = _Writer()
    c._sess = pair_common._Session(w, b"k" * 32, listener=False)
    c._sess.hello = True
    c._task = asyncio.get_running_loop().create_future()      # цикл подключения «идёт»
    monkeypatch.setattr(pairlink, "_link", c)
    await pairlink.drop_session()
    assert w.closed, "сессия клиента не закрыта"
    assert c._task is not None and not c._task.done(), "по «down» остановлен цикл подключения"
    c._task.cancel()


async def test_drop_without_a_link_or_a_session_is_a_no_op(monkeypatch, caplog):
    """Живость сказала «недоступен», а закрывать нечего: без линка и без сессии —
    ни исключения, ни строки «сессия закрыта» в журнале, слушатель на месте."""
    monkeypatch.setattr(pairlink, "_link", None)
    with caplog.at_level(logging.INFO):
        await pairlink.drop_session()
        srv = pair_server.PairServer(object())
        monkeypatch.setattr(pairlink, "_link", srv)
        await pairlink.drop_session()
        await pairlink.drop_session()
    assert pairlink._link is srv and srv._sess is None and not srv.online
    assert not [r for r in caplog.records if "сессия закрыта" in r.getMessage()], \
        "без сессии журнал сообщает о её закрытии"
