"""Слой runtime кворума — то, чего нет в test_quorum_runtime и test_quorum_channels:
слова кворума своему шлюзу по каналу конфигурации (linkserver/deliver.send_quorum),
исполнители действий агента обхода Telegram и перерезолва (runtime/guestlink:
`tg`, `tg_assert`, `reresolve`, `link` с незнакомым видом), привязка ответчика
к интерфейсу AWG и его отказы (runtime/quorum).

Домен, каналы, скрипт линка, сессия Bot API — подмены с журналом; сокеты
ответчика — настоящие UDP на 127.0.0.1 (как в test_quorum_runtime), привязка к
интерфейсу — запись вызова `setsockopt` подменного класса сокета.

Цена ошибки: слово кворума, ушедшее шлюзу другим видом или вовсе неизвестное, —
агент рвёт сессию канала конфигурации (незнакомый вид — разрыв); правило метки
обхода без новой сессии Bot API — живые соединения агента остаются на мёртвом
аплинке; повтор правила метки, пересоздающий сессию каждые 10 минут, — рваные
долгие опросы Telegram; ответчик без привязки к awg0 — подделанная проба с eth0
(без туннеля зонда) засчитывается как голос; падение домена на пробе или ответе
роняет цикл сокетов кворума целиком.
"""
from __future__ import annotations

import asyncio
import socket
import types

import pytest

from awgbot.domain.gateway.reserve import uplink as upmod
from awgbot.infra.gwguard import rsv as rsvlink
from awgbot.runtime import guestlink, tgsession
from awgbot.runtime import quorum as rq
from awgbot.runtime.linkserver.deliver import DeliverMixin

pytestmark = pytest.mark.unit

KINDS = ("qask", "qtake", "qrel", "qneed", "qpause", "qstate")


# ── send_quorum ─────────────────────────────────────────────────────────────

class _Srv(DeliverMixin):
    def __init__(self, ok: bool = True):
        self.sent: list[tuple] = []
        self.ok = ok

    async def send(self, slot_id, kind, body=None, pad=0):
        self.sent.append((slot_id, kind, body))
        return self.ok


@pytest.mark.parametrize("kind", KINDS)
async def test_each_quorum_word_goes_to_its_gateway_under_its_own_kind(kind):
    srv = _Srv()
    body = {"l": "a", "e": 12}
    assert await srv.send_quorum(2, kind, body) is True
    assert srv.sent == [(2, kind, body)], f"{kind}: ушло не тем видом или не тому слоту: {srv.sent}"


@pytest.mark.parametrize("kind", ["qview", "qshare", "qmem", "state", "", "QTAKE"])
async def test_an_unknown_quorum_word_is_not_sent_at_all(kind):
    srv = _Srv()
    assert await srv.send_quorum(1, kind, {"l": "a"}) is False
    assert srv.sent == [], f"«{kind}» ушло шлюзу — агент порвал бы сессию на незнакомом виде"


async def test_a_word_that_did_not_go_reports_false():
    srv = _Srv(ok=False)
    assert await srv.send_quorum(1, "qtake", {"l": "a", "e": 1}) is False


# ── исполнители агента: обход Telegram и перерезолв ─────────────────────────

class _Bot:
    def __init__(self, fail: bool = False):
        self.session = _Session("old")
        self.get_me_calls = 0
        self.fail = fail

    async def get_me(self):
        self.get_me_calls += 1
        if self.fail:
            raise TimeoutError("нет ответа")
        return types.SimpleNamespace(username="gw_bot")


class _Session:
    def __init__(self, name: str):
        self.name = name
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture()
def agent_rt(monkeypatch):
    runs: list[tuple] = []
    bot = _Bot()
    monkeypatch.setattr(guestlink, "_bot", bot)
    monkeypatch.setattr(rsvlink, "run", lambda mode, *a, **k: runs.append((mode, *a)) or "ok")
    made: list[_Session] = []

    def make(**kw):
        s = _Session(f"new{len(made)}")
        made.append(s)
        return s
    monkeypatch.setattr(tgsession, "make", make)
    return types.SimpleNamespace(runs=runs, bot=bot, made=made)


async def test_switching_the_bypass_on_marks_the_sockets_renews_the_session_and_asks_get_me(agent_rt):
    old = agent_rt.bot.session
    await guestlink.run_actions(types.SimpleNamespace(), [("tg", "on")])
    assert agent_rt.runs == [("tg", "on")], agent_rt.runs
    assert agent_rt.bot.session is agent_rt.made[0] and old.closed, "сессия Bot API не пересоздана — соединения на прежнем пути"
    assert agent_rt.bot.get_me_calls == 1, "после включения обхода getMe не спрошен"


async def test_switching_the_bypass_off_renews_the_session_without_get_me(agent_rt):
    await guestlink.run_actions(types.SimpleNamespace(), [("tg", "off")])
    assert agent_rt.runs == [("tg", "off")] and len(agent_rt.made) == 1
    assert agent_rt.bot.get_me_calls == 0


async def test_a_silent_telegram_after_switching_on_does_not_break_the_actions(agent_rt, monkeypatch):
    monkeypatch.setattr(guestlink, "_bot", _Bot(fail=True))
    done = []

    class _Svc:
        def reserve_reresolve_done(self, p, res):
            done.append(res)
    monkeypatch.setattr(upmod, "reresolve_now", lambda *a, **k: "set")
    await guestlink.run_actions(_Svc(), [("tg", "on"), ("reresolve", {"name": "vpn.example.org", "expect": "198.51.100.7"})])
    assert done == ["set"], "молчащий getMe оборвал следующие действия"


async def test_the_mark_assert_reruns_the_rule_and_keeps_the_session(agent_rt):
    old = agent_rt.bot.session
    await guestlink.run_actions(types.SimpleNamespace(), [("tg_assert", "on")])
    assert agent_rt.runs == [("tg", "on")]
    assert agent_rt.bot.session is old and not old.closed and agent_rt.made == [], \
        "подтверждение правила метки пересоздало сессию Bot API"


async def test_a_failing_mark_script_does_not_stop_the_rest(agent_rt, monkeypatch):
    def boom(mode, *a, **k):
        raise rsvlink.RsvError("nft не принял")
    monkeypatch.setattr(rsvlink, "run", boom)
    pushed = []
    from awgbot.runtime import linkclient

    class _Lc:
        async def push(self):
            pushed.append(1)
    monkeypatch.setattr(linkclient, "ensure", lambda services: _Lc())
    await guestlink.run_actions(types.SimpleNamespace(), [("tg", "on"), ("push",)])
    assert pushed == [1], "отказ скрипта метки оборвал снимок серверу"


async def test_the_reresolve_asks_the_record_and_reports_the_result(monkeypatch):
    calls, done = [], []
    monkeypatch.setattr(upmod, "reresolve_now", lambda *a, **k: calls.append(a) or "same")

    class _Svc:
        def reserve_reresolve_done(self, p, res):
            done.append((p, res))
    p = {"name": "vpn-a.example.org", "expect": "198.51.100.7", "why": "переезд", "move": 3}
    await guestlink.run_actions(_Svc(), [("reresolve", p)])
    assert calls == [("vpn-a.example.org", "198.51.100.7", "переезд", 3)], calls
    assert done == [(p, "same")], "исход перерезолва не вернулся домену — повод не закроется"


async def test_a_reresolve_with_a_garbage_reason_still_reports(monkeypatch):
    done = []
    monkeypatch.setattr(upmod, "reresolve_now", lambda *a, **k: None)

    class _Svc:
        def reserve_reresolve_done(self, p, res):
            done.append(res)
    await guestlink.run_actions(_Svc(), [("reresolve", None)])
    assert done == [""], done


async def test_a_link_word_of_an_unknown_kind_is_not_sent(monkeypatch):
    from awgbot.runtime import linkclient
    sent = []

    class _Lc:
        _writer = object()

        async def _send(self, kind, body):
            sent.append(kind)
    monkeypatch.setattr(linkclient, "ensure", lambda services: _Lc())
    await guestlink.run_actions(types.SimpleNamespace(), [("link", "qshare", {"l": "a"}), ("link", "qview", {"l": "a"})])
    assert sent == ["qview"], f"незнакомое слово ушло серверу по каналу конфигурации: {sent}"


# ── ответчик: привязка к интерфейсу и отказы ────────────────────────────────

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Svc:
    def __init__(self):
        self.plan = {"responder": None, "probes": {}}
        self.probes_in: list = []
        self.boom = False

    def quorum_runtime_plan(self):
        return self.plan

    def quorum_tick(self):
        return []

    def quorum_probe_in(self, data, src):
        self.probes_in.append(data)
        if self.boom:
            raise ValueError("домен упал")
        return b"REPLY"

    def quorum_reply_in(self, name, data):
        raise ValueError("домен упал")


def _sock_ns(monkeypatch, *, fail: bool = False) -> list[tuple]:
    calls: list[tuple] = []

    class _Rec(socket.socket):
        def setsockopt(self, level, opt, value):
            if level == socket.SOL_SOCKET and opt == 25:
                calls.append((level, opt, value))
                if fail:
                    raise PermissionError("нужен CAP_NET_RAW")
                return None
            return super().setsockopt(level, opt, value)
    monkeypatch.setattr(rq, "socket", types.SimpleNamespace(socket=_Rec, AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM,
                                                            SOL_SOCKET=socket.SOL_SOCKET))
    return calls


async def _ask(port: int, data: bytes = b'{"q":1}') -> list[bytes]:
    loop = asyncio.get_running_loop()
    got: list[bytes] = []

    class _P(asyncio.DatagramProtocol):
        def datagram_received(self, d, addr):
            got.append(d)
    tr, _ = await loop.create_datagram_endpoint(_P, local_addr=("127.0.0.1", 0))
    tr.sendto(data, ("127.0.0.1", port))
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.01)
    tr.close()
    return got


async def test_the_responder_is_bound_to_the_awg_interface_of_the_plan(monkeypatch):
    calls = _sock_ns(monkeypatch)
    svc = _Svc()
    port = _free_port()
    svc.plan = {"responder": ("127.0.0.1", port), "probes": {}, "responder_if": "awg0"}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    try:
        assert calls == [(socket.SOL_SOCKET, 25, b"awg0\x00")], f"ответчик не привязан к интерфейсу AWG: {calls}"
        assert await _ask(port) == [b"REPLY"]
    finally:
        await rt.stop()


async def test_without_an_interface_in_the_plan_the_responder_is_not_bound(monkeypatch):
    """Бот основы на резерве живёт в зоне — интерфейса в плане нет, привязки тоже."""
    calls = _sock_ns(monkeypatch)
    svc = _Svc()
    port = _free_port()
    svc.plan = {"responder": ("127.0.0.1", port), "probes": {}}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    try:
        assert calls == [] and await _ask(port) == [b"REPLY"]
    finally:
        await rt.stop()


async def test_a_refused_binding_still_brings_the_responder_up(monkeypatch):
    calls = _sock_ns(monkeypatch, fail=True)
    svc = _Svc()
    port = _free_port()
    svc.plan = {"responder": ("127.0.0.1", port), "probes": {}, "responder_if": "awg0"}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    try:
        assert calls and await _ask(port) == [b"REPLY"], "отказ привязки оставил лидера без ответчика"
    finally:
        await rt.stop()


async def test_a_busy_address_is_retried_on_the_next_tick():
    busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    busy.bind(("127.0.0.1", 0))
    port = busy.getsockname()[1]
    svc = _Svc()
    svc.plan = {"responder": ("127.0.0.1", port), "probes": {}}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    try:
        assert rt._resp is None, "ответчик на занятом адресе считается поднятым"
        busy.close()
        await rt.tick()
        assert rt._resp is not None and await _ask(port) == [b"REPLY"], "освободившийся адрес не взят на следующем такте"
    finally:
        await rt.stop()


async def test_a_failing_domain_on_a_probe_answers_nothing_and_keeps_listening():
    svc = _Svc()
    svc.boom = True
    port = _free_port()
    svc.plan = {"responder": ("127.0.0.1", port), "probes": {}}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    try:
        assert await _ask(port) == []
        svc.boom = False
        assert await _ask(port) == [b"REPLY"], "после падения домена на пробе ответчик замолчал"
    finally:
        await rt.stop()


async def test_a_failing_domain_on_a_reply_runs_no_actions():
    ran = []

    async def act(a):
        ran.append(a)
    prober = rq._Prober(_Svc(), "awgqah", act)
    await prober._in(b"{}")
    assert ran == [], "падение разбора ответа дало действия"
