"""Клиенты каналов внутри своих туннелей — канал конфигурации шлюза
(runtime/linkclient), канал пары (runtime/pairlink/client.py), гостевой канал
(runtime/guestlink.py): пределы подключения и первой строки — константами
`gwlink.CONNECT_S` / `gwlink.FIRST_LINE_S` (8 и 15 с, не прежние 20), а сокет,
открытый до `try`, не остаётся без close, если до ответа второй стороны сорвался
шаг в потоке (сборка hello) или drain.

Цена ошибки: 20 с на мёртвый коннект — лишние секунды до повтора на каждом
обрыве линка; незакрытый сокет на каждой неудачной попытке — утечка дескрипторов
у процесса, который живёт неделями и подключается с бэкоффом сутками.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from awgbot.runtime import guestlink
from awgbot.runtime import linkclient
from awgbot.runtime.pairlink import client as pair_client
from awgbot.util import gwlink

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[2]
CLIENTS = ("awgbot/runtime/linkclient/session.py", "awgbot/runtime/pairlink/client.py",
           "awgbot/runtime/guestlink.py")


class _Sock:
    """Сокет, по которому вторая сторона молчит: читать нечего, drain по желанию висит."""

    def __init__(self, *, hang_drain: bool = False):
        self.written: list[bytes] = []
        self.closed = False
        self.hang_drain = hang_drain
        self.transport = self

    async def readline(self) -> bytes:
        await asyncio.Event().wait()

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        if self.hang_drain:
            await asyncio.Event().wait()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def abort(self) -> None:
        self.closed = True

    def get_extra_info(self, name):
        return None


def _patch_open(monkeypatch, module, sock: _Sock | None):
    """open_connection модуля: отдаёт sock (None — висит, как дропнутый SYN)."""
    async def open_connection(host, port, limit=None):
        if sock is None:
            await asyncio.Event().wait()
        return sock, sock
    monkeypatch.setattr(module.asyncio, "open_connection", open_connection)


def _patch_wait_for(monkeypatch, module) -> list:
    """wait_for модуля: записывает запрошенный предел, ждёт не дольше 0.05 с."""
    seen: list = []
    real = asyncio.wait_for

    async def fast(aw, timeout=None):
        seen.append(timeout)
        return await real(aw, 0.05 if timeout is not None else None)
    monkeypatch.setattr(module.asyncio, "wait_for", fast)
    return seen


# ── пределы ──────────────────────────────────────────────────────────────────

def test_the_tunnel_limits_are_short_and_live_in_one_place():
    """Соединение ≤ 10 с (правило «наружу»), первая строка — 15 с: она идёт после
    рукопожатия AWG, если линк простаивал."""
    assert 0 < gwlink.CONNECT_S <= 10
    assert gwlink.CONNECT_S < gwlink.FIRST_LINE_S <= 15
    assert guestlink.HELLO_TIMEOUT == gwlink.FIRST_LINE_S


def test_no_client_waits_for_the_tunnel_with_a_literal_twenty():
    """Сторож по исходникам трёх клиентов: wait_for вокруг open_connection и
    readline — с пределом из gwlink, а не числом (прежние 20 возвращались бы
    правкой одного файла незаметно)."""
    bad = []
    for rel in CLIENTS:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "wait_for"):
                continue
            inner = ast.unparse(node.args[0]) if node.args else ""
            if "open_connection" not in inner and "readline" not in inner:
                continue
            limit = next((k.value for k in node.keywords if k.arg == "timeout"),
                         node.args[1] if len(node.args) > 1 else None)
            if limit is None or isinstance(limit, ast.Constant):
                bad.append(f"{rel}:{node.lineno}: {ast.unparse(node)[:90]}")
    assert not bad, "предел туннеля числом или без предела:\n" + "\n".join(bad)


async def test_the_pair_client_gives_up_a_dead_connect_after_connect_s(monkeypatch):
    class Svc:
        def reserve_connect_address(self): return ("10.99.98.1", 8788)
        def reserve_channel_secret(self): return "A" * 43 + "="
    _patch_open(monkeypatch, pair_client, None)
    seen = _patch_wait_for(monkeypatch, pair_client)
    with pytest.raises(asyncio.TimeoutError):
        await pair_client.PairClient(Svc())._connect_once()
    assert seen[:1] == [gwlink.CONNECT_S], f"коннект канала пары ждёт {seen}"


async def test_the_pair_client_waits_for_the_first_line_first_line_s(monkeypatch):
    class Svc:
        def reserve_connect_address(self): return ("10.99.98.1", 8788)
        def reserve_channel_secret(self): return "A" * 43 + "="
        def reserve_accept_hello_body(self): return None
        def reserve_hello_body(self): return {"proto": 1}
    sock = _Sock()
    _patch_open(monkeypatch, pair_client, sock)
    seen = _patch_wait_for(monkeypatch, pair_client)
    with pytest.raises(asyncio.TimeoutError):
        await pair_client.PairClient(Svc())._connect_once()
    assert gwlink.FIRST_LINE_S in seen, f"первая строка канала пары ждётся {seen}"
    assert sock.closed, "сокет после молчания второй стороны не закрыт"


async def test_the_link_client_uses_the_shared_limits(monkeypatch, tmp_path):
    from awgbot.core import config
    from tests.unit._linkclient_session_common import PRIV, _Agent
    f = tmp_path / "awglink.conf"
    f.write_text(f"[Interface]\nPrivateKey = {PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(f))
    monkeypatch.setattr(linkclient, "server_address", lambda: "10.99.99.1")
    sock = _Sock()
    _patch_open(monkeypatch, linkclient, sock)
    seen = _patch_wait_for(monkeypatch, linkclient)
    with pytest.raises(asyncio.TimeoutError):
        await linkclient.LinkClient(_Agent())._connect_once()
    assert seen[:2] == [gwlink.CONNECT_S, gwlink.FIRST_LINE_S], f"пределы канала конфигурации: {seen}"
    assert sock.closed


# ── сокет без close ──────────────────────────────────────────────────────────

async def test_the_pair_client_closes_the_socket_when_hello_cannot_be_built(monkeypatch):
    """Сбой в потоке между open_connection и try (сборка hello из reserve.json):
    прежде сокет оставался открытым до конца процесса."""
    class Svc:
        def reserve_connect_address(self): return ("10.99.98.1", 8788)
        def reserve_channel_secret(self): return "A" * 43 + "="
        def reserve_accept_hello_body(self): raise RuntimeError("reserve.json занят")
    sock = _Sock()
    _patch_open(monkeypatch, pair_client, sock)
    with pytest.raises(RuntimeError):
        await pair_client.PairClient(Svc())._connect_once()
    assert sock.closed, "сокет канала пары утёк после сбоя сборки hello"


async def test_the_guest_client_closes_the_socket_when_the_hello_drain_hangs(monkeypatch):
    """Гостевой канал: drain hello не уложился в срок до того, как _writer
    выставлен — _close() его не видит, закрыть обязан сам _connect_once."""
    class Svc:
        def reserve_guest_target(self): return ("10.99.97.1", 8789)
        def reserve_guest_channel_key(self): return b"k" * 32
        def reserve_guest_hello_body(self): return {"slot": 1}
    sock = _Sock(hang_drain=True)
    _patch_open(monkeypatch, guestlink, sock)
    monkeypatch.setattr(guestlink, "SEND_TIMEOUT", 0.05)
    c = guestlink.GuestClient(Svc())
    with pytest.raises(asyncio.TimeoutError):
        await c._connect_once()
    assert sock.closed, "сокет гостевого канала утёк после зависшего drain"
    assert c._writer is None and c.online is False


async def test_the_guest_client_closes_the_socket_when_there_is_no_view_yet(monkeypatch):
    class Svc:
        def reserve_guest_target(self): return ("10.99.97.1", 8789)
        def reserve_guest_channel_key(self): return b"k" * 32
        def reserve_guest_hello_body(self): return {}
    sock = _Sock()
    _patch_open(monkeypatch, guestlink, sock)
    with pytest.raises(OSError):
        await guestlink.GuestClient(Svc())._connect_once()
    assert sock.closed and sock.written == []
