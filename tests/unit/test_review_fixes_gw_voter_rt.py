"""Правки по ревью — слой runtime голосующих: ответчик лидера и такт кворума агента шлюза.

- ответчик лидера с привязкой к интерфейсу AWG пересоздаётся, когда у интерфейса новый
  индекс (`awg-quick down/up` при тех же адресе и порте);
- такт кворума агента — отдельное задание планировщика со своим ритмом: медленные шаги
  наблюдателей резерва (подъём линка, DNS, уведомления, обход Telegram) пробы не задерживают.

Сокеты ответчика — настоящие UDP на 127.0.0.1, привязка к интерфейсу — запись вызова
`setsockopt` подменного класса сокета, индекс интерфейса — подмена `if_nametoindex`.

Цена ошибки: сокет ответчика держит индекс прежнего интерфейса — пробы с нового ему не
доставляются, все голосующие «не видят» лидера, ложный «мёртв» и переезд при живой основе;
проба голосующего шлюза ждёт медленных шагов наблюдателей дольше no_s — ложное «не вижу»
кандидату и обязательство «нет» на pledge_s.
"""
from __future__ import annotations

import asyncio
import socket
import types

import pytest

from awgbot.runtime import quorum as rq

pytestmark = pytest.mark.unit


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Svc:
    def __init__(self, port: int):
        self.plan = {"responder": ("127.0.0.1", port), "probes": {}, "responder_if": "awg0"}

    def quorum_runtime_plan(self):
        return self.plan

    def quorum_tick(self):
        return []

    def quorum_probe_in(self, data, src):
        return b"REPLY"

    def quorum_reply_in(self, name, data):
        return []


@pytest.fixture()
def ifidx(monkeypatch):
    """Индекс awg0 — по словарю; привязка к устройству — запись (на стенде без CAP_NET_RAW)."""
    idx = {"awg0": 7}
    binds: list[bytes] = []

    class _Rec(socket.socket):
        def setsockopt(self, level, opt, value):
            if level == socket.SOL_SOCKET and opt == 25:
                binds.append(value)
                return None
            return super().setsockopt(level, opt, value)

    def nametoindex(name):
        if name not in idx or not idx[name]:
            raise OSError(19, "No such device")
        return idx[name]
    monkeypatch.setattr(rq, "socket", types.SimpleNamespace(
        socket=_Rec, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM, SOL_SOCKET=socket.SOL_SOCKET,
        if_nametoindex=nametoindex))
    return types.SimpleNamespace(idx=idx, binds=binds)


async def _ask(port: int) -> list[bytes]:
    loop = asyncio.get_running_loop()
    got: list[bytes] = []

    class _P(asyncio.DatagramProtocol):
        def datagram_received(self, d, addr):
            got.append(d)
    tr, _ = await loop.create_datagram_endpoint(_P, local_addr=("127.0.0.1", 0))
    tr.sendto(b"x", ("127.0.0.1", port))
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.01)
    tr.close()
    return got


async def test_a_new_index_of_the_awg_interface_recreates_the_responder(ifidx):
    port = _free_port()
    rt = rq.QuorumRuntime(_Svc(port), lambda a: asyncio.sleep(0))
    try:
        await rt.tick()
        first = rt._resp[1]
        await rt.tick()
        assert rt._resp[1] is first, "ответчик пересоздан без смены интерфейса — дребезг сокета каждый такт"
        ifidx.idx["awg0"] = 9                                # awg-quick down/up: тот же адрес, новый индекс
        await rt.tick()
        assert rt._resp is not None and rt._resp[1] is not first, \
            "сокет ответчика держит индекс прежнего awg0 — пробы с нового интерфейса не доходят"
        assert ifidx.binds == [b"awg0\x00", b"awg0\x00"], "новый сокет не привязан к интерфейсу заново"
        assert await _ask(port) == [b"REPLY"], "пересозданный ответчик не отвечает"
    finally:
        await rt.stop()


async def test_a_missing_interface_keeps_the_responder_until_it_is_back(ifidx):
    port = _free_port()
    rt = rq.QuorumRuntime(_Svc(port), lambda a: asyncio.sleep(0))
    try:
        await rt.tick()
        first = rt._resp[1]
        ifidx.idx["awg0"] = 0                                # интерфейс опущен — новый сокет не поднять
        await rt.tick()
        assert rt._resp[1] is first, "ответчик закрыт на опущенном интерфейсе — после подъёма шум отказов"
        ifidx.idx["awg0"] = 11
        await rt.tick()
        assert rt._resp[1] is not first, "интерфейс вернулся с новым индексом, а ответчик прежний"
    finally:
        await rt.stop()


async def test_without_a_way_to_learn_the_index_the_responder_is_kept(monkeypatch):
    """Индекс узнать нечем (нет if_nametoindex) — прежнее поведение: сокет не трогается."""
    monkeypatch.setattr(rq, "socket", types.SimpleNamespace(
        socket=socket.socket, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM, SOL_SOCKET=socket.SOL_SOCKET))
    port = _free_port()
    svc = _Svc(port)
    svc.plan["responder_if"] = ""
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    try:
        await rt.tick()
        first = rt._resp[1]
        await rt.tick()
        assert rt._resp[1] is first
    finally:
        await rt.stop()
