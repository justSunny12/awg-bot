"""Сокеты кворума резерва (runtime/quorum.py): ответчик лидера и зонды
голосующего по плану домена — концепт «кворум» §3.1, §3.2, §4.10.

Настоящие UDP-сокеты на 127.0.0.1 и петля asyncio; домен — `_Svc` (план,
такт, ответ на пробу, разбор ответа — моделью); вход в пространство `awg-quo`
(`netns.listen_in`) подменён вызовом `make()` на месте — сокет зонда
создаётся тем же путём, что в бою, только без setns.

Цена ошибки: ответчик не на адресе плана — пробы уходят в пустоту, лидер
лишается кворума при живой сети; ответчик, не закрытый при смене плана, —
второй процесс на том же адресе не поднимется; сокет зонда, созданный не
через пространство, — проба уходит маршрутом хоста мимо туннеля зонда;
повтор неудачного создания сокета на каждом такте — шум в журнале и лишняя
работа каждые 5 с.
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from awgbot.infra import quoprobe
from awgbot.runtime import quorum as rq
from awgbot.util import netns

pytestmark = pytest.mark.unit


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Svc:
    def __init__(self):
        self.plan = {"responder": None, "probes": {}}
        self.tick_actions: list[tuple] = []
        self.probes_in: list[tuple[bytes, str]] = []
        self.replies_in: list[tuple[str, bytes]] = []
        self.reply = b"REPLY"

    def quorum_runtime_plan(self):
        return self.plan

    def quorum_tick(self):
        out, self.tick_actions = self.tick_actions, []
        return out

    def quorum_probe_in(self, data, src):
        self.probes_in.append((data, src))
        return self.reply

    def quorum_reply_in(self, name, data):
        self.replies_in.append((name, data))
        return [("send", "qview", {"from": name})]


@pytest.fixture()
def made(monkeypatch):
    """Сокеты зондов: через listen_in пространства awg-quo — журнал путей."""
    paths: list[str] = []

    def listen_in(path, make):
        paths.append(path)
        return make()
    monkeypatch.setattr(netns, "listen_in", listen_in)
    monkeypatch.setattr(quoprobe, "ns_path", lambda: "/run/netns/awg-quo")
    return paths


async def _until(cond, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("не дождались")
        await asyncio.sleep(0.01)


async def test_the_responder_answers_a_probe_on_the_planned_address(made):
    svc, acts = _Svc(), []

    async def act(a):
        acts.extend(a)
    port = _free_port()
    svc.plan = {"responder": ("127.0.0.1", port), "probes": {}}
    rt = rq.QuorumRuntime(svc, act)
    await rt.tick()
    try:
        loop = asyncio.get_running_loop()
        got: list[bytes] = []

        class _P(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                got.append(data)
        tr, _ = await loop.create_datagram_endpoint(_P, local_addr=("127.0.0.1", 0))
        tr.sendto(b'{"q":1}', ("127.0.0.1", port))
        await _until(lambda: got)
        assert got == [b"REPLY"], "ответ на пробу не вернулся отправителю"
        assert svc.probes_in[0][0] == b'{"q":1}' and svc.probes_in[0][1] == "127.0.0.1", \
            "домену не отдан адрес источника — узел не сверить"
        tr.close()
    finally:
        await rt.stop()


async def test_no_reply_from_the_domain_sends_nothing(made):
    svc = _Svc()
    svc.reply = None
    port = _free_port()
    svc.plan = {"responder": ("127.0.0.1", port), "probes": {}}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    try:
        loop = asyncio.get_running_loop()
        got: list[bytes] = []

        class _P(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                got.append(data)
        tr, _ = await loop.create_datagram_endpoint(_P, local_addr=("127.0.0.1", 0))
        tr.sendto(b"x", ("127.0.0.1", port))
        await _until(lambda: svc.probes_in)
        await asyncio.sleep(0.05)
        assert got == [], "ответ на непризнанную пробу"
        tr.close()
    finally:
        await rt.stop()


async def test_a_plan_without_a_responder_closes_it(made):
    svc = _Svc()
    port = _free_port()
    svc.plan = {"responder": ("127.0.0.1", port), "probes": {}}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    assert rt._resp is not None
    svc.plan = {"responder": None, "probes": {}}
    await rt.tick()
    assert rt._resp is None, "ответчик остался после плана без него"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port))                     # адрес свободен — сокет правда закрыт
    s.close()


async def test_a_new_responder_address_moves_the_socket(made):
    svc = _Svc()
    p1, p2 = _free_port(), _free_port()
    svc.plan = {"responder": ("127.0.0.1", p1), "probes": {}}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    svc.plan = {"responder": ("127.0.0.1", p2), "probes": {}}
    await rt.tick()
    try:
        assert rt._resp[0] == ("127.0.0.1", p2)
    finally:
        await rt.stop()


async def test_probes_go_out_of_the_probe_socket_and_replies_reach_the_executor(made):
    svc, acts = _Svc(), []

    async def act(a):
        acts.extend(a)
    loop = asyncio.get_running_loop()
    seen: list[tuple[bytes, tuple]] = []

    class _Resp(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.t = transport

        def datagram_received(self, data, addr):
            seen.append((data, addr))
            self.t.sendto(b"ANSWER", addr)
    tr, _ = await loop.create_datagram_endpoint(_Resp, local_addr=("127.0.0.1", 0))
    resp_port = tr.get_extra_info("sockname")[1]
    svc.plan = {"responder": None, "probes": {"awgqbh": {"node_addr": "127.0.0.1", "resp": "127.0.0.1", "port": resp_port}}}
    svc.tick_actions = [("probe", "awgqbh", b"PROBE", ("127.0.0.1", resp_port)), ("send", "qprobe", {"l": "a"})]
    rt = rq.QuorumRuntime(svc, act)
    await rt.tick()
    try:
        assert made == ["/run/netns/awg-quo"], "сокет зонда создан не в пространстве awg-quo"
        await _until(lambda: svc.replies_in)
        assert seen[0][0] == b"PROBE", "проба не ушла в сокет зонда"
        assert svc.replies_in == [("awgqbh", b"ANSWER")], "ответ не отдан домену с именем зонда"
        await _until(lambda: ("send", "qview", {"from": "awgqbh"}) in acts)
        assert ("send", "qprobe", {"l": "a"}) in acts, "прочие действия такта не дошли до исполнителя"
        assert not [a for a in acts if a[0] == "probe"], "проба ушла исполнителю каналов вместо сокета"
    finally:
        tr.close()
        await rt.stop()


async def test_a_probe_for_a_socket_that_is_not_up_is_dropped_quietly(made):
    svc, acts = _Svc(), []

    async def act(a):
        acts.extend(a)
    svc.tick_actions = [("probe", "awgqzz", b"P", ("127.0.0.1", 9))]
    rt = rq.QuorumRuntime(svc, act)
    await rt.tick()
    assert acts == []
    await rt.stop()


async def test_a_failed_probe_socket_is_retried_not_sooner_than_30_seconds(monkeypatch):
    calls: list[str] = []

    def listen_in(path, make):
        calls.append(path)
        raise netns.NetnsError("пространства ещё нет")
    monkeypatch.setattr(netns, "listen_in", listen_in)
    monkeypatch.setattr(quoprobe, "ns_path", lambda: "/run/netns/awg-quo")
    svc = _Svc()
    svc.plan = {"responder": None, "probes": {"awgqah": {"node_addr": "127.0.0.1", "resp": "127.0.0.1", "port": 9}}}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    await rt.tick()
    await rt.tick()
    assert len(calls) == 1, f"сокет зонда пересоздаётся на каждом такте: {len(calls)}"
    rt._failed["awgqah"] -= 31
    await rt.tick()
    assert len(calls) == 2, "после 30 с повтора нет"
    await rt.stop()


async def test_a_probe_that_left_the_plan_closes_its_socket(made):
    svc = _Svc()
    svc.plan = {"responder": None, "probes": {"awgqah": {"node_addr": "127.0.0.1", "resp": "127.0.0.1", "port": 9}}}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    assert set(rt._probes) == {"awgqah"}
    svc.plan = {"responder": None, "probes": {}}
    await rt.tick()
    assert rt._probes == {}, "сокет снятого зонда остался"
    await rt.stop()


async def test_a_failing_plan_or_tick_does_not_raise(made):
    class _Bad(_Svc):
        def quorum_runtime_plan(self):
            raise RuntimeError("база недоступна")
    rt = rq.QuorumRuntime(_Bad(), lambda a: asyncio.sleep(0))
    await rt.tick()

    class _BadTick(_Svc):
        def quorum_tick(self):
            raise RuntimeError("такт упал")
    rt = rq.QuorumRuntime(_BadTick(), lambda a: asyncio.sleep(0))
    await rt.tick()
    await rt.stop()


async def test_the_module_tick_keeps_one_runtime_and_shutdown_closes_it(made):
    svc = _Svc()
    port = _free_port()
    svc.plan = {"responder": ("127.0.0.1", port), "probes": {}}
    await rq.shutdown()
    try:
        await rq.tick(svc, lambda a: asyncio.sleep(0))
        first = rq.runtime()
        await rq.tick(svc, lambda a: asyncio.sleep(0))
        assert rq.runtime() is first, "новый рантайм на каждом такте — сокеты пересоздаются"
    finally:
        await rq.shutdown()
    assert rq.runtime() is None
