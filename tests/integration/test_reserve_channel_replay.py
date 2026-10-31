"""Канал пары, слушатель (runtime/pairlink/server.py) на живых сокетах: повтор
перехваченного `hello` и чужой адрес внутри линка.

`hello` обычной сессии подписан ключом линка, но не нонсом слушателя — его
можно повторить байт в байт. Новая сессия вытесняет живую только первым
словом клиента, подписанным нонсом слушателя этой сессии; обычная сессия —
только с адреса второй стороны /30 линка пары.

Цена ошибки: повтор `hello` изнутри линка рвал живую сессию пары (новая
вытесняла прежнюю сразу) — каждые несколько секунд, сколько угодно долго:
ни пакета, ни переезда, ни кворума; в домене сессия открывалась заново
(сброс отметок журнала и кворума) по слову того, кто ключа не знает.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.runtime.pairlink import server as server_mod
from awgbot.util import pairproto

from tests.integration import _reserve_common as rc
from tests.integration.test_gwlink_channel import _Gw, _until
from tests.integration.test_reserve_channel import _closed, linked, server   # noqa: F401  (фикстуры)

pytestmark = pytest.mark.integration
rsv = rc.rsv

HELLO = {"name": "Beta", "version": "4.0.0", "caps": [], "gen": 0, "ports": [45871], "seen": "",
         "bot": "beta_vpn_bot", "gw": True}


async def _live(server, key) -> _Gw:
    gw = _Gw(*await asyncio.open_connection(*server._bound), key=key)
    await gw.send("hello", HELLO)
    assert (await gw.recv())["t"] == "st"
    await gw.send("ping", {"n": "abcdef0123456789"})
    assert (await gw.recv())["t"] == "pong"
    assert await _until(lambda: server.online)
    return gw


async def test_a_replayed_hello_without_a_signed_word_leaves_the_live_session(services, server, linked, monkeypatch):
    monkeypatch.setattr(server_mod, "HELLO_TIMEOUT", 0.5)
    live = await _live(server, linked)
    sess = server._sess
    opened = []
    real = services.reserve_session_opened
    monkeypatch.setattr(services, "reserve_session_opened", lambda hello: opened.append(1) or real(hello))
    # те же байты hello — повтор перехваченного; нонса слушателя у повторяющего нет
    reader, writer = await asyncio.open_connection(*server._bound)
    seq = live.seq
    writer.write(live.pack("hello", HELLO, seq=1))
    live.seq = seq                                                     # счёт живой сессии — свой
    await writer.drain()
    await asyncio.wait_for(reader.readline(), 2)                       # st (подпись — не его забота)
    assert server._sess is sess, "повтор hello вытеснил живую сессию до первого подписанного слова"
    assert await asyncio.wait_for(reader.readline(), 2) == b"", "сессия повтора не закрылась без подписанного слова"
    assert server._sess is sess and server.online and not opened, "сессия открыта в домене по повтору"
    await live.send("ping", {"n": "0123456789abcdef"})
    assert (await live.recv())["t"] == "pong", "живая сессия разорвана повтором"
    writer.close()
    await live.close()


async def test_a_new_session_that_proves_itself_takes_over(services, server, linked):
    old = await _live(server, linked)
    first = server._sess
    new = await _live(server, linked)
    assert server._sess is not first
    assert await _closed(old), "прежняя сессия не закрыта новой, подписавшей слово"
    await new.close()


async def test_an_ordinary_session_from_another_address_is_refused(services, server, linked, monkeypatch):
    acc, _inv = pairproto.pair_hosts(services.reserve_peer()["pair_cidr"])
    assert type(services).reserve_acceptor_address(services) == acc
    monkeypatch.setattr(services, "reserve_acceptor_address", lambda: acc)    # петля — не адрес второй стороны
    gw = _Gw(*await asyncio.open_connection(*server._bound), key=linked)
    await gw.send("hello", HELLO)
    assert await _closed(gw), "обычная сессия с чужого адреса внутри линка"
    assert not server.online
