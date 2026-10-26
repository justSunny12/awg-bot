"""Слова гостя и блокировки в локальном канале (runtime/pairlink/guestctl.py,
этап переезда и возврата): `tg_taken`, `back_req`, `replica` копятся у сервера
для такта; `blocks` доходят до гостя и накладываются в зоне.

Цена ошибки: потерянное `tg_taken` — резерв не узнает, что вебхук поставлен,
основа ждёт `taken` и откатывает переезд, а Telegram уже у гостя; слово,
выданное дважды, — второе `taken` и второе уведомление; реплика, принятая
как слово без проверки на пустоту, — слияние на пустой базе; блокировки,
не дошедшие до гостя, — заблокированный на основе пользователь ходит через
резерв беспрепятственно.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import types

import pytest

from awgbot.runtime.pairlink import guestctl
from tests.integration.test_reserve_guestctl import _ResSvc, _GuestSvc, _until

pytestmark = pytest.mark.integration


class _Guest(_GuestSvc):
    def __init__(self):
        super().__init__()
        self.applied: list[list[str]] = []

    def guest_apply_blocks(self, addrs):
        self.applied.append(list(addrs))


@pytest.fixture()
async def pair():
    d = tempfile.mkdtemp(prefix="rg", dir="/tmp")
    path = os.path.join(d, "g.sock")
    res, gst = _ResSvc(), _Guest()
    server = guestctl.GuestCtlServer(res, path)
    client = guestctl.GuestCtlClient(gst, path)
    await server.ensure()
    client.start()
    assert await _until(lambda: server.connected() and client.connected())
    yield types.SimpleNamespace(server=server, client=client, guest=gst)
    await client.stop()
    await server.stop()
    shutil.rmtree(d, ignore_errors=True)


async def test_the_words_of_the_guest_are_kept_in_order_and_taken_once(pair):
    assert await pair.client.send("tg_taken")
    assert await pair.client.send("replica", {"blob": "0a0b"})
    assert await pair.client.send("back_req")
    assert await _until(lambda: len(pair.server._words) == 3)
    assert pair.server.take_words() == [("tg_taken", None), ("replica", b"\x0a\x0b"), ("back_req", None)]
    assert pair.server.take_words() == [], "слово выдано дважды — второе taken и второе уведомление"


@pytest.mark.parametrize("blob", ["", "zz"])
async def test_an_empty_or_broken_replica_is_not_a_word(pair, blob):
    assert await pair.client.send("replica", {"blob": blob})
    await asyncio.sleep(0.3)
    assert pair.server.take_words() == [], "пустая реплика стала словом — слияние пошло бы по пустой базе"


async def test_blocks_of_the_primary_reach_the_guest_and_are_applied(pair):
    assert await pair.server.send("blocks", {"addrs": ["10.8.1.2", "10.8.1.9"]})
    assert await _until(lambda: pair.guest.applied)
    assert pair.guest.applied[-1] == ["10.8.1.2", "10.8.1.9"]
    assert pair.client.blocks == ["10.8.1.2", "10.8.1.9"]


async def test_an_empty_blocks_list_is_delivered_too_so_unblocking_is_not_lost(pair):
    assert await pair.server.send("blocks", {"addrs": []})
    assert await _until(lambda: pair.guest.applied == [[]])


async def test_the_client_registry_returns_what_main_registered():
    guestctl.set_client(None)
    assert guestctl.client() is None
    sentinel = object()
    guestctl.set_client(sentinel)
    try:
        assert guestctl.client() is sentinel
    finally:
        guestctl.set_client(None)
