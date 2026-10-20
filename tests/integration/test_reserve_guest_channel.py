"""Гостевой канал целиком на петле: слушатель резерва
(runtime/pairlink/guest.GuestChannelServer) и клиент шлюза
(runtime/guestlink.GuestClient) — настоящие сокеты, настоящий конверт
gwlink с ключом из PSK; сервисы обеих сторон — журналы.

Пространство зоны не нужно: сокет слушателя — на 127.0.0.1, ключ и слот по
адресу (`_key_for`) — подмена с тем же PSK, что у клиента.

Цена ошибки: первое слово резерва без его нонса — клиент рвёт сессию на
каждом подключении; вторая сессия того же слота, не вытеснившая первую, —
два голоса одного шлюза; слово шлюза, которое слушатель не принимает, —
разрыв сессии и выход шлюза из набора голосующих.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.runtime import guestlink
from awgbot.runtime.pairlink import guest as guestsrv
from awgbot.util import pairproto, wgkeys

pytestmark = pytest.mark.integration

PSK = wgkeys.psk()


class _Reserve:
    """Сервисы резерва: журнал слов шлюзов и ответ на hello — view и state."""

    def __init__(self):
        self.opened: list[tuple] = []
        self.words: list[tuple] = []
        self.closed: list[int] = []

    def reserve_guest_opened(self, slot, body):
        self.opened.append((slot, dict(body)))
        return [("gw", slot, "view", {"seen": "", "voters": [], "in": "idle", "pkg_gen": 3}),
                ("gw", slot, "state", {"state": "idle", "move": 0})]

    def reserve_guest_in(self, slot, kind, body):
        self.words.append((slot, kind, dict(body)))
        return []

    def reserve_guest_closed(self, slot):
        self.closed.append(slot)
        return []


class _Gateway:
    def __init__(self, port: int, slot: int = 1):
        self.port, self.slot = port, slot
        self.got: list[tuple] = []

    def reserve_guest_target(self):
        return "127.0.0.1", self.port

    def reserve_guest_channel_key(self):
        return pairproto.guest_key(PSK)

    def reserve_guest_hello_body(self):
        return {"slot": self.slot, "caps": ["rsv1"], "m_gen": 1, "seen": ""}

    def reserve_guest_in(self, kind, body):
        self.got.append((kind, dict(body)))
        if kind == "ping":
            return [("guest", "pong", {"n": body["n"]})]
        return []


async def _until(pred, timeout=5.0) -> bool:
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


@pytest.fixture()
async def loop_pair(monkeypatch):
    res = _Reserve()
    srv = guestsrv.GuestChannelServer(res, 0)
    monkeypatch.setattr(srv, "_key_for", lambda addr: (pairproto.guest_key(PSK), 1))
    server = await asyncio.start_server(srv._serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(guestsrv, "_server", srv)
    gw = _Gateway(port)
    cl = guestlink.GuestClient(gw)
    monkeypatch.setattr(guestlink, "_client", cl)
    cl.start()
    assert await _until(lambda: cl.online and srv.online_slots() == [1]), "сессия гостевого канала не открылась"
    yield res, srv, gw, cl
    await cl.stop()
    await srv.stop()
    server.close()
    await server.wait_closed()


async def test_hello_opens_the_session_and_the_reserve_speaks_first_with_its_nonce(loop_pair):
    res, srv, gw, cl = loop_pair
    assert res.opened and res.opened[0][0] == 1 and res.opened[0][1]["m_gen"] == 1
    assert await _until(lambda: [k for k, _ in gw.got] == ["view", "state"]), f"шлюз не получил view и state: {gw.got}"


async def test_a_ping_of_the_reserve_is_answered_with_a_pong_of_the_same_nonce(loop_pair):
    res, srv, gw, cl = loop_pair
    assert await srv.send(1, "ping", {"n": "0123456789abcdef"})
    assert await _until(lambda: (1, "pong", {"n": "0123456789abcdef"}) in res.words), f"pong не пришёл: {res.words}"


async def test_words_of_the_gateway_reach_the_reserve(loop_pair):
    res, srv, gw, cl = loop_pair
    assert await cl.send("seen", {"seen": "2026-10-21T03:11:00+03:00"})
    assert await _until(lambda: any(k == "seen" for _, k, _ in res.words))
    assert srv.online_slots() == [1] and not res.closed


async def test_the_gateways_own_probe_of_the_reserve_does_not_break_the_session(loop_pair):
    """Шлюз пробует резерв `ping` по гостевому каналу, когда awgrsv молчит
    (domain/gateway/reserve/watch). Слушатель резерва обязан это пережить:
    разрыв — шлюз вне набора голосующих по одной потерянной keepalive."""
    res, srv, gw, cl = loop_pair
    await guestlink.run_actions(None, [("guest", "ping", {"n": "fedcba9876543210"})])
    await asyncio.sleep(0.3)
    assert srv.online_slots() == [1] and not res.closed, "проба шлюза порвала гостевой канал у резерва"
    assert await _until(lambda: ("pong", {"n": "fedcba9876543210"}) in gw.got, 2.0), "на пробу шлюза резерв не ответил"


async def test_a_second_session_of_the_same_slot_replaces_the_first(loop_pair):
    res, srv, gw, cl = loop_pair
    other = guestlink.GuestClient(_Gateway(gw.port))
    other.start()
    try:
        assert await _until(lambda: len(res.opened) == 2)
        assert await _until(lambda: not cl.online or cl._writer is None, 3.0), "прежняя сессия слота не вытеснена"
        assert srv.online_slots() == [1]
    finally:
        await other.stop()
