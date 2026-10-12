"""
Общее для test_linkclient_session_*.py: ВПС на том конце (`_Vps`, `_Conn`),
агент шлюза (`_Agent`, `_CfgAgent`), открытая сессия (`_live_client`),
фикстуры конфига линка, роли шлюза и крючков чата.

Клиент канала на шлюзе (runtime/linkclient): жизнь сессии — бэкофф, ключ,
таймаут коннекта, когда клиент заводится, переподключение по восстановлению линка, снимок
сразу после операции из чата агента.

Сети нет: ВПС на том конце — `_Vps`, подставленный вместо
`asyncio.open_connection`, он отдаёт заранее записанные строки и записывает,
что ему прислали. Всё остальное — боевой код клиента, включая настоящий
`_connect_once` и цикл `_run`.

Цена ошибок здесь — ритм в линке: сессия, которую сервер закрывает сразу
(чужой ключ, чужой слот), без роста шага
превращается в стук каждые пять секунд — ровный маячок внутри туннеля и запись
на SD на каждом коннекте. И обратное — полуоткрытая сессия после жёсткого
ребута ВПС, которая висит «на связи» днями и глушит своё скачивание фидов.
"""
from __future__ import annotations

import asyncio
import base64
import os

import pytest

from awgbot.core import config
from awgbot.domain.channelstate import ChannelState
from awgbot.infra import gwguard
from tests.linkstubs import AgentStub
from awgbot.runtime import linkclient
from awgbot.util import gwlink


PRIV = base64.b64encode(os.urandom(32)).decode()
PRIV2 = base64.b64encode(os.urandom(32)).decode()
KEY = gwlink.channel_key(PRIV)


class _Agent(AgentStub):
    """Агент шлюза: ровно то, что клиент канала у него спрашивает."""

    def __init__(self):
        self.snap = {"bundle": {"lan_mode": "1"}, "egress_ok": True, "agent_version": "3.1.0"}
        self.roles: list[bool] = []
        self.caps: list = []                      # что сервер сказал о своих возможностях
        self.link_up = True
        self.touched = 0
        self.channel = ChannelState()

    def lan_feeds_applied_hash(self) -> str:
        return ""

    def gw_snapshot(self) -> dict:
        return dict(self.snap)

    def gateway_claim_if_needed(self):
        return None

    def lan_feeds_touch(self, digest: str = "") -> None:
        self.touched += 1

    def set_link_role(self, active: bool, standby=None, name=None, caps=None, server=...) -> None:
        self.roles.append(active)
        self.caps.append(caps)

    # для on_tick: что показал последний тик монитора
    def cached_status(self, max_age):
        return object()

    def link_ok(self, st) -> bool:
        if isinstance(self.link_up, Exception):
            raise self.link_up
        return self.link_up


class _Conn:
    """Одна TCP-сессия с ВПС: читает записанные строки, потом EOF (или висит)."""

    def __init__(self, lines, hang: bool = False):
        self.lines = list(lines)
        self.hang = hang
        self.written: list[bytes] = []
        self.closed = False
        self._gone = asyncio.Event()

    async def readline(self) -> bytes:
        if self.lines:
            line = self.lines.pop(0)
            # ответ сервера собирается по hello, который клиент уже прислал:
            # подпись — нонсом клиента, первое слово несёт нонс сервера
            return line(self) if callable(line) else line
        if self.hang:
            await self._gone.wait()               # сессия открыта, ВПС молчит
        return b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self._gone.set()

    async def wait_closed(self) -> None:
        pass

    def hello(self, key: bytes = KEY) -> dict:
        """Разобранный hello, пришедший в эту сессию (подписан без нонса)."""
        return gwlink.unpack(key, self.written[0])


SN = b"S" * gwlink.NONCE_BYTES            # нонс сервера в записанных сессиях


def _reply(key: bytes, kind: str, body: dict, *, seq: int = 1, first: bool = True,
           sign_key: bytes | None = None):
    """Строка сервера для этой сессии: подписана нонсом из hello клиента,
    первая несёт нонс сервера — как у боевого слушателя."""
    def make(conn: "_Conn") -> bytes:
        cn = gwlink.nonce_from(conn.hello(key).get("nonce"))
        out = dict(body)
        if first:
            out["nonce"] = gwlink.nonce_b64(SN)
        return gwlink.pack(sign_key or key, kind, out, seq=seq, nonce=cn)
    return make


class _Vps:
    """ВПС на том конце: очередь сессий. Каждое подключение забирает следующую."""

    def __init__(self, monkeypatch):
        self.sessions: list[_Conn] = []
        self.connects = 0

        async def open_connection(host, port, limit=None):
            self.connects += 1
            conn = self.sessions.pop(0)
            return conn, conn

        monkeypatch.setattr(linkclient.asyncio, "open_connection", open_connection)
        monkeypatch.setattr(linkclient, "server_address", lambda: "10.99.99.1")
        monkeypatch.setattr(linkclient, "server_port", lambda: gwlink.DEFAULT_PORT)

    def hangs_up(self) -> _Conn:
        """Сервер закрыл сразу, не сказав ни слова: чужая подпись, чужой слот."""
        c = _Conn([])
        self.sessions.append(c)
        return c

    def answers(self, key: bytes = KEY, active: bool = True, caps=None, **kw) -> _Conn:
        """Настоящая сессия: сервер сказал роль (первое, что он говорит после hello);
        caps — возможности сервера в этом слове (None — сервер прежний, поля нет)."""
        body = {"active": active} if caps is None else {"active": active, "caps": caps}
        c = _Conn([_reply(key, "role", body)], **kw)
        self.sessions.append(c)
        return c

    def forged(self) -> _Conn:
        """На том конце не наш ВПС: подпись чужим ключом."""
        other = gwlink.channel_key(base64.b64encode(os.urandom(32)).decode())
        c = _Conn([_reply(KEY, "role", {"active": True}, sign_key=other)])
        self.sessions.append(c)
        return c


@pytest.fixture()
def conf(tmp_path, monkeypatch):
    f = tmp_path / "awglink.conf"
    f.write_text(f"[Interface]\nPrivateKey = {PRIV}\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(f))
    return f


@pytest.fixture()
def vps(monkeypatch, conf):
    return _Vps(monkeypatch)


def _delays(monkeypatch, stop_after: int) -> list[float]:
    """Паузы между попытками; после `stop_after`-й цикл останавливается."""
    got: list[float] = []

    async def fake_sleep(seconds):
        got.append(seconds)
        if len(got) >= stop_after:
            raise asyncio.CancelledError

    monkeypatch.setattr(linkclient.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(linkclient.random, "uniform", lambda lo, hi: 1.0)
    return got


@pytest.fixture()
def gateway_role(monkeypatch):
    """Шлюз с юнитом обвязки без единой строки канала: канал обязателен, и
    держится он конфигом линка, а не переключателем в юните."""
    monkeypatch.setattr(config, "ROLE", "gateway")
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
    monkeypatch.setattr(linkclient, "_client", None)
    monkeypatch.setattr(linkclient, "_conf_gone_since", None)
    yield
    c = linkclient._client
    if c is not None and c._task is not None:
        c._task.cancel()


class _Wire(_Conn):
    """Сокет открытой сессии для тестов тика: записывает всё, что ушло."""

    def __init__(self):
        super().__init__([])


def _live_client(agent) -> linkclient.LinkClient:
    """Клиент с «открытой» сессией и задачей, которую ensure() не перезапустит."""
    client = linkclient.LinkClient(agent)
    client._writer = _Wire()
    client._sn = SN                              # сессия открыта: нонс сервера известен
    client._task = asyncio.get_running_loop().create_future()
    linkclient._client = client
    return client


class _CfgAgent(_Agent):
    """Агент с документом: apply_cfg отдаёт заданный итог и считает вызовы,
    очередь ответа — в памяти (как ключ в БД агента)."""

    def __init__(self, result: dict | None = None):
        super().__init__()
        self.pending: dict = {}
        self.docs: list[dict] = []
        self.result = result or {"ack": {"gen": 7, "hash": "ab" * 8, "applied": ["env"], "wait": {},
                                         "declined": {}, "failed": {}}, "note": "", "ask": []}

    def apply_cfg(self, msg: dict) -> dict:
        self.docs.append(msg)
        return self.result

    def cfg_ack_pending_put(self, ack: dict) -> None:
        self.pending = {**ack, "at": "2026-10-10T12:00:00+03:00"}

    def cfg_ack_pending_get(self) -> dict:
        return dict(self.pending)

    def cfg_ack_pending_clear(self) -> None:
        self.pending = {}

    def cfg_answer_ack(self, answer: dict):
        if not answer.get("ok"):
            return None
        return {"gen": answer["gen"], "hash": answer["hash"], "applied": [answer["section"]],
                "wait": {}, "declined": {}, "failed": {}}


@pytest.fixture()
def cfg_hooks(monkeypatch):
    """Крючки уведомления и вопроса — записывают, что ушло человеку."""
    got = {"notes": [], "asks": []}

    async def notify(text):
        got["notes"].append(text)

    async def ask(section):
        got["asks"].append(section)
    monkeypatch.setattr(linkclient, "_notify", notify)
    monkeypatch.setattr(linkclient, "_ask", ask)
    return got


def _sent(client) -> list[dict]:
    return [gwlink.unpack(KEY, x, nonce=SN) for x in client._writer.written]
