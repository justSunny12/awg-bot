"""Локальный канал гостя (runtime/pairlink/guestctl.py): отправка с пределом на
drain — вторая сторона не читает (песочница гостя остановлена, бот резерва
завис) → слово не ушло, сокет закрыт, сессия считается мёртвой; hello гостя
собирается в потоке (число устройств — awg show, журнал — база), а слушатель
проверяет пару базой тоже в потоке.

Цена ошибки: drain без предела — такт бота резерва (push_state из тика) или
такт гостя (send_online) висит вместе с сокетом, за ним встают остальные
задания планировщика; awg show в цикле событий — паузы в ответах бота на
каждое подключение гостя.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from awgbot.runtime.pairlink import guestctl

pytestmark = pytest.mark.unit


class _Writer:
    def __init__(self, *, hang: bool):
        self.hang = hang
        self.written: list[bytes] = []
        self.closed = False

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        if self.hang:
            await asyncio.Event().wait()

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("side", ["server", "client"])
async def test_a_drain_that_hangs_closes_the_socket_and_reports_false(monkeypatch, side):
    monkeypatch.setattr(guestctl, "SEND_TIMEOUT", 0.05)
    obj = guestctl.GuestCtlServer(object(), "/tmp/x.sock") if side == "server" \
        else guestctl.GuestCtlClient(object(), "/tmp/x.sock")
    w = _Writer(hang=True)
    obj._writer = w
    assert await asyncio.wait_for(obj.send("state", {"a": 1}), 2) is False, "зависший drain сошёл за отправку"
    assert w.closed, "сокет после зависшего drain остался открытым — сессия полуживая"
    assert obj.connected() is False


async def test_a_quick_drain_is_an_ordinary_send():
    obj = guestctl.GuestCtlServer(object(), "/tmp/x.sock")
    w = _Writer(hang=False)
    obj._writer = w
    assert await obj.send("take_tg") is True
    assert w.written == [b'{"kind": "take_tg"}\n'] and not w.closed


def test_the_drain_limit_is_within_the_rule():
    assert 0 < guestctl.SEND_TIMEOUT <= 10


async def test_the_guest_hello_is_built_off_the_event_loop(monkeypatch):
    """guest_devices_online (awg show) и guest_jrn_status (база) — в потоке."""
    loop_thread = threading.get_ident()
    threads: dict[str, int] = {}

    class Svc:
        def guest_devices_online(self):
            threads["online"] = threading.get_ident()
            return 2

        def guest_jrn_status(self):
            threads["jrn"] = threading.get_ident()
            return {"e": 1}

    w = _Writer(hang=False)

    async def open_unix_connection(path, limit=None):
        class R:
            async def readline(self):
                return b""
        return R(), w
    monkeypatch.setattr(guestctl.asyncio, "open_unix_connection", open_unix_connection)
    c = guestctl.GuestCtlClient(Svc(), "/tmp/x.sock")
    task = asyncio.get_running_loop().create_task(c._run())
    for _ in range(100):
        if w.written:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert w.written and b'"kind": "hello"' in w.written[0]
    assert threads and all(t != loop_thread for t in threads.values()), \
        f"hello гостя собирается в цикле событий: {threads}"


async def test_the_listener_checks_the_pair_off_the_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: list[int] = []

    class Svc:
        def reserve_paired(self):
            seen.append(threading.get_ident())
            return False
    monkeypatch.setattr(guestctl, "_server", None)
    assert await guestctl.ensure(Svc()) is None
    assert seen and seen[0] != loop_thread, "reserve_paired (база) зовётся в цикле событий"
