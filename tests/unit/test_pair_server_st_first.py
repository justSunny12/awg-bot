"""Канал пары, слушатель (runtime/pairlink/server.py `_serve_session`, `send`):
первое слово обычной сессии — своё `st`; на связи (`online`) сессия только
после него, а слово, отправленное в это окно (задания кворума `qmem`/`qprobe`/
`qview` с такта), ждёт `st` и уходит за ним.

Цена ошибки: клиент ждёт `st` первым и рвёт сессию на любом другом слове —
слово такта, попавшее в окно между `hello` и `st` (запись в базу при
открытии сессии), давало вечный круг переподключений.
"""
from __future__ import annotations

import asyncio
import socket
import time

import pytest

from awgbot.runtime.pairlink import common as plc
from awgbot.runtime.pairlink.server import PairServer
from awgbot.util import gwlink

pytestmark = pytest.mark.unit

KEY = b"k" * 32


class _Svc:
    def reserve_session_opened(self, hello):
        time.sleep(0.3)                                    # запись в базу при открытии сессии
        return []

    def reserve_st_body_sent(self):
        return {"x": 1}

    def reserve_session_closed(self):
        pass

    def reserve_last_word(self):
        return []


async def _pair():
    a, b = socket.socketpair()
    r1, w1 = await asyncio.open_connection(sock=a)
    r2, w2 = await asyncio.open_connection(sock=b)
    return (r1, w1), (r2, w2)


async def test_a_word_sent_before_st_goes_after_it():
    (r1, w1), (r2, w2) = await _pair()
    srv = PairServer(_Svc())
    sess = plc._Session(w1, KEY, listener=True)
    sess.cn = gwlink.new_nonce()
    task = asyncio.create_task(srv._serve_session(r1, sess, {"caps": []}, 0))
    try:
        await asyncio.sleep(0.05)
        assert not srv.online, "на связи до своего st"
        sent = asyncio.create_task(srv.send("qview", {"l": "a"}))
        st = gwlink.unpack(KEY, await asyncio.wait_for(r2.readline(), 2), nonce=sess.cn)
        assert st.get("t") == "st", f"первым ушло не st: {st.get('t')}"
        # клиент отвечает словом, подписанным нонсом сессии, — сессия открыта
        w2.write(gwlink.pack(KEY, "ping", {"n": "abcdef0123456789"}, seq=1, nonce=gwlink.nonce_from(st["nonce"])))
        assert await asyncio.wait_for(sent, 2) is True
        kinds = {gwlink.unpack(KEY, await asyncio.wait_for(r2.readline(), 2), nonce=sess.cn).get("t") for _ in range(2)}
        assert kinds == {"qview", "pong"}
        assert srv.online
    finally:
        task.cancel()
        w2.close()
        await srv.stop()


async def test_a_word_waiting_for_st_of_a_session_that_died_is_not_sent():
    (r1, w1), (r2, w2) = await _pair()
    srv = PairServer(_Svc())
    sess = plc._Session(w1, KEY, listener=True)
    sess.cn = gwlink.new_nonce()
    srv._sess = sess                                       # сессия открыта, st ещё не ушло
    pending = asyncio.create_task(srv.send("qview", {"l": "a"}))
    await asyncio.sleep(0.05)
    srv._sess = None
    sess.ready.set()                                       # сессия кончилась (finally _serve)
    assert await asyncio.wait_for(pending, 2) is False
    w1.close()
    w2.close()
