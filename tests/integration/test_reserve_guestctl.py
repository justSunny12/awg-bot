"""Локальный канал бот резерва ↔ бот основы на резерве (runtime/pairlink/
guestctl.py) по настоящему unix-сокету: состояние вниз, команды вниз, число
устройств и реплика вверх.

Цена ошибки: сокет с правами шире 0600 — любой процесс на резерве командует
ботом основы (`stop` снимает реплику, `take_tg` берёт право отвечать в
Telegram); состояние, пересылаемое на каждый такт, — маячок внутри хоста и
лишняя запись; команда `stop`, не дошедшая до гостя, — возврат клиентов
зависает; реплика не шифротекстом — открытая база клиентов основы живёт в
памяти бота резерва, а он принадлежит другому человеку.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import types

import pytest

from awgbot.runtime.pairlink import guestctl

pytestmark = pytest.mark.integration


async def _until(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


class _ResSvc:
    """Сервисы бота резерва: что сказать гостю."""

    def __init__(self):
        self.view = {"name": "Бета", "since": "2026-10-21T03:12:00+05:00", "reason": "dead",
                     "primary": {"status": "dead"}, "return": "immediate", "own_name": "Альфа", "self_host": ""}
        self.calls = 0

    def reserve_guest_state_view(self):
        self.calls += 1
        return dict(self.view)

    def reserve_paired(self):
        return True


class _GuestSvc:
    def __init__(self, online=3):
        self.states: list[dict] = []
        self.online = online

    def guest_state_in(self, body):
        self.states.append(dict(body))

    def guest_devices_online(self):
        return self.online


@pytest.fixture()
async def pair():
    # путь сокета unix ограничен ~100 знаками: каталог pytest слишком длинный
    d = tempfile.mkdtemp(prefix="rg", dir="/tmp")
    path = os.path.join(d, "g.sock")
    res, gst = _ResSvc(), _GuestSvc()
    server = guestctl.GuestCtlServer(res, path)
    client = guestctl.GuestCtlClient(gst, path)
    await server.ensure()
    client.start()
    yield types.SimpleNamespace(server=server, client=client, res=res, guest=gst, path=path)
    await client.stop()
    await server.stop()
    shutil.rmtree(d, ignore_errors=True)


async def test_the_socket_is_private_to_the_owner(pair):
    assert stat.S_IMODE(os.stat(pair.path).st_mode) == 0o600


async def test_a_new_guest_gets_the_state_and_the_reserve_learns_the_online_count(pair):
    assert await _until(lambda: pair.guest.states and pair.server.online_count == 3), "приветствие не дошло"
    assert pair.guest.states[0]["name"] == "Бета" and pair.guest.states[0]["primary"] == {"status": "dead"}
    assert pair.server.connected() and pair.client.connected()


async def test_the_state_is_sent_on_change_and_not_repeated_while_it_stays_the_same(pair):
    await _until(lambda: pair.guest.states)
    n = len(pair.guest.states)
    for _ in range(3):
        assert await pair.server.push_state() is True
    await asyncio.sleep(0.2)
    assert len(pair.guest.states) == n, "одно и то же состояние переслано снова"
    pair.res.view["primary"] = {"status": "alive"}
    assert await pair.server.push_state() is True
    assert await _until(lambda: len(pair.guest.states) == n + 1)
    assert pair.guest.states[-1]["primary"] == {"status": "alive"}
    assert await pair.server.push_state(force=True) is True
    assert await _until(lambda: len(pair.guest.states) == n + 2), "force обязан переслать"


async def test_the_online_count_is_reported_to_the_reserve(pair):
    await _until(lambda: pair.server.connected())
    pair.guest.online = 11
    assert await pair.client.send_online() is True
    assert await _until(lambda: pair.server.online_count == 11)


async def test_commands_of_the_reserve_reach_the_guest(pair):
    await _until(lambda: pair.server.connected())
    assert not pair.client.take_tg.is_set() and not pair.client.stop_req.is_set()
    assert await pair.server.send("take_tg") is True
    assert await _until(pair.client.take_tg.is_set)
    assert not pair.client.stop_req.is_set(), "take_tg не должна останавливать гостя"
    assert await pair.server.send("stop") is True
    assert await _until(pair.client.stop_req.is_set)


async def test_the_replica_arrives_as_the_bytes_the_guest_sent(pair):
    await _until(lambda: pair.server.connected())
    cipher = bytes(range(256)) * 40
    assert await pair.client.send_replica(cipher) is True
    assert await _until(lambda: pair.server.replica == cipher)


async def test_a_replica_that_is_not_hex_is_dropped_not_stored(pair):
    await _until(lambda: pair.server.connected())
    pair.server.replica = b"old"
    pair.client._writer.write(b'{"kind": "replica", "blob": "zz-not-hex"}\n')
    await pair.client._writer.drain()
    assert await _until(lambda: pair.server.replica is None)


async def test_garbage_lines_and_unknown_kinds_are_ignored_and_the_session_survives(pair):
    await _until(lambda: pair.server.connected())
    w = pair.client._writer
    w.write(b"not json\n[1]\n" + '{"kind": "что-то-новое"}\n'.encode() + b'{"no_kind": 1}\n')
    await w.drain()
    pair.guest.online = 7
    assert await pair.client.send_online() is True
    assert await _until(lambda: pair.server.online_count == 7), "сессия оборвалась на мусоре"


async def test_the_back_request_of_the_guest_is_remembered_with_its_time(pair):
    await _until(lambda: pair.server.connected())
    assert pair.server.back_requested_at == 0.0
    assert await pair.client.send("back_req") is True
    assert await _until(lambda: pair.server.back_requested_at > 0)


async def test_sending_without_a_session_reports_false_instead_of_raising(tmp_path):
    srv = guestctl.GuestCtlServer(_ResSvc(), str(tmp_path / "x.sock"))
    assert await srv.send("state", {}) is False and await srv.push_state() is False and srv.connected() is False
    cli = guestctl.GuestCtlClient(_GuestSvc(), str(tmp_path / "x.sock"))
    assert await cli.send("online", {}) is False and cli.connected() is False


async def test_the_guest_reconnects_after_the_reserve_bot_restarts(pair):
    await _until(lambda: pair.guest.states)
    await pair.server.stop()
    assert await _until(lambda: not pair.client.connected(), 3)
    fresh = guestctl.GuestCtlServer(pair.res, pair.path)
    await fresh.ensure()
    try:
        assert await _until(lambda: fresh.connected(), 15), "гость не вернулся к перезапущенному боту резерва"
        assert fresh.online_count == 3
    finally:
        await fresh.stop()
    pair.server = fresh


async def test_a_failing_state_view_does_not_break_the_channel(pair):
    await _until(lambda: pair.server.connected())

    def boom():
        raise RuntimeError("нет данных")
    pair.res.reserve_guest_state_view = boom
    assert await pair.server.push_state(force=True) is False
    assert pair.server.connected()


async def test_stopping_the_server_removes_the_socket_file(pair):
    await pair.server.stop()
    assert not os.path.exists(pair.path)


async def test_the_listener_exists_only_while_the_pair_does(tmp_path, monkeypatch):
    d = tempfile.mkdtemp(prefix="rg", dir="/tmp")
    try:
        from awgbot.infra import rsvzone
        monkeypatch.setattr(rsvzone, "GUEST_SOCK", type(tmp_path)(d) / "g.sock")
        paired = {"on": True}
        svc = types.SimpleNamespace(reserve_paired=lambda: paired["on"], reserve_guest_state_view=lambda: {})
        srv = await guestctl.ensure(svc)
        assert srv is not None and os.path.exists(srv.path)
        paired["on"] = False
        assert await guestctl.ensure(svc) is None and guestctl.server() is None
        assert not os.path.exists(os.path.join(d, "g.sock")), "без пары слушатель остался"
    finally:
        await guestctl.shutdown()
        shutil.rmtree(d, ignore_errors=True)



async def test_the_webhook_word_of_the_guest_is_kept_for_the_reserve_with_typed_fields(pair):
    await _until(lambda: pair.server.connected())
    assert await pair.client.send("webhook", {"stalled": True, "since": "2026-10-21T03:12:00+05:00",
                                              "error": "Connection timed out"}) is True
    assert await pair.client.send("webhook", {"stalled": False}) is True
    words = []
    assert await _until(lambda: (words.extend(pair.server.take_words()) or True) and len(words) >= 2)
    assert words == [("webhook", {"stalled": True, "since": "2026-10-21T03:12:00+05:00", "error": "Connection timed out"}),
                     ("webhook", {"stalled": False, "since": "", "error": ""})], words
