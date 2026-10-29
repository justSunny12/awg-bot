"""Каналы кворума резерва в слое runtime (концепт «кворум» §3.3, §4.4): входы
канала пары (`_in_q*` → домен), локальный канал гостя (`quorum` боту основы на
резерве, `qcount` боту резерва, `push_quorum` по смене), исполнители действий
(`("guestctl", …)` у бота основы на резерве, `("link", …)` у агента шлюза).

Домен и каналы — подмены: проверяется, что слово доходит туда, куда должно,
один раз и с тем же телом.

Цена ошибки: вид канала пары без обработчика — слово второго сервера молча
пропадает (`qprobe` не принят — зонда к его серверу нет вовсе); состав гостю
на каждом такте — шум в локальном канале; `qcount` не доходит до бота резерва —
карточка второй стороны без счёта; `qview` агента не уходит по каналу
конфигурации — сервер не знает, видит ли его свой шлюз.
"""
from __future__ import annotations

import types

import pytest

from awgbot.runtime import guestlink, rsvactions
from awgbot.runtime.pairlink import guestctl
from awgbot.runtime.pairlink.inbound import InboundMixin

pytestmark = pytest.mark.unit

QUORUM_PAIR_KINDS = ("qmem", "qmem_ok", "qprobe", "qkeys", "qkeys_ok", "qview", "qstate", "qpause")


def test_every_quorum_pair_kind_has_a_handler():
    missing = [k for k in QUORUM_PAIR_KINDS if not callable(getattr(InboundMixin, f"_in_{k}", None))]
    assert not missing, f"виды канала пары без обработчика: {missing}"


@pytest.mark.parametrize("kind", QUORUM_PAIR_KINDS)
async def test_a_quorum_pair_word_reaches_the_domain_and_its_actions_run(monkeypatch, kind):
    seen, ran = [], []

    class _Svc:
        def reserve_quorum_pair_in(self, k, body):
            seen.append((k, body))
            return [("send", "qview", {"echo": k})]

    async def run(actions):
        ran.extend(actions)
    monkeypatch.setattr(rsvactions, "run", run)
    link = InboundMixin.__new__(InboundMixin)
    link.services = _Svc()
    await getattr(link, f"_in_{kind}")(None, {"l": "b"})
    assert seen == [(kind, {"l": "b"})], f"слово {kind} не дошло до домена как есть"
    assert ran == [("send", "qview", {"echo": kind})], "действия домена не исполнены"


# ── локальный канал гостя ────────────────────────────────────────────────────

class _GSvc:
    def __init__(self):
        self.qcount, self.quorum = [], []
        self.body = {"l": "b", "g": 2, "members": ["b", "a", "b1"], "port": 8789}

    def reserve_quorum_qcount_in(self, msg):
        self.qcount.append(msg)

    def reserve_quorum_guest_in(self, msg):
        self.quorum.append(msg)

    def reserve_quorum_for_guest(self):
        return self.body


async def test_qcount_from_the_guest_reaches_the_reserve_domain():
    svc = _GSvc()
    srv = guestctl.GuestCtlServer.__new__(guestctl.GuestCtlServer)
    srv.services = svc
    srv._words = []
    await srv._handle({"kind": "qcount", "y": 2, "v": 3, "r": "lead"})
    assert svc.qcount == [{"kind": "qcount", "y": 2, "v": 3, "r": "lead"}]


async def test_the_line_from_the_reserve_reaches_the_guest_domain():
    svc = _GSvc()
    cl = guestctl.GuestCtlClient.__new__(guestctl.GuestCtlClient)
    cl.services = svc
    await cl._handle({"kind": "quorum", "l": "b", "g": 2, "members": ["b", "a", "b1"], "port": 8789})
    assert svc.quorum and svc.quorum[0]["members"] == ["b", "a", "b1"]


async def test_push_quorum_sends_once_per_change_and_again_on_force():
    svc = _GSvc()
    srv = guestctl.GuestCtlServer.__new__(guestctl.GuestCtlServer)
    srv.services = svc
    sent = []

    async def send(kind, body=None):
        sent.append((kind, body))
        return True
    srv.send = send
    assert await srv.push_quorum()
    assert await srv.push_quorum()
    assert sent == [("quorum", svc.body)], f"состав гостю без смены: {sent}"
    await srv.push_quorum(force=True)
    assert len(sent) == 2, "hello гостя — состав не переслан"
    svc.body = {**svc.body, "g": 3}
    await srv.push_quorum()
    assert sent[-1][1]["g"] == 3


async def test_push_quorum_with_nothing_to_say_sends_nothing():
    svc = _GSvc()
    svc.body = None
    srv = guestctl.GuestCtlServer.__new__(guestctl.GuestCtlServer)
    srv.services = svc
    srv.send = lambda *a, **k: pytest.fail("состав без состава")
    assert await srv.push_quorum() is False


async def test_a_failed_push_is_retried_next_time():
    svc = _GSvc()
    srv = guestctl.GuestCtlServer.__new__(guestctl.GuestCtlServer)
    srv.services = svc
    sent = []
    ok = [False]

    async def send(kind, body=None):
        sent.append(kind)
        return ok[0]
    srv.send = send
    await srv.push_quorum()
    ok[0] = True
    await srv.push_quorum()
    assert sent == ["quorum", "quorum"], "неотправленный состав считается отправленным"


# ── исполнители действий ─────────────────────────────────────────────────────

async def test_the_guestctl_action_goes_to_the_reserve_bot_over_the_local_channel(monkeypatch):
    sent = []

    class _Cl:
        async def send(self, kind, body=None):
            sent.append((kind, body))
            return True
    monkeypatch.setattr(guestctl, "client", lambda: _Cl())
    await rsvactions.run([("guestctl", "qcount", {"y": 1, "v": 3, "r": "lead"})])
    assert sent == [("qcount", {"y": 1, "v": 3, "r": "lead"})]


async def test_the_guestctl_action_without_a_channel_is_dropped_quietly(monkeypatch):
    monkeypatch.setattr(guestctl, "client", lambda: None)
    await rsvactions.run([("guestctl", "qcount", {"y": 1})])


async def test_the_link_action_of_the_agent_goes_over_the_config_channel(monkeypatch):
    from awgbot.runtime import linkclient
    sent = []

    class _Lc:
        _writer = object()

        async def _send(self, kind, body):
            sent.append((kind, body))
    monkeypatch.setattr(linkclient, "ensure", lambda services: _Lc())
    await guestlink.run_actions(types.SimpleNamespace(), [("link", "qview", {"l": "a", "w": "see"})])
    assert sent == [("qview", {"l": "a", "w": "see"})]


async def test_the_link_action_without_a_session_sends_nothing(monkeypatch):
    from awgbot.runtime import linkclient

    class _Lc:
        _writer = None

        async def _send(self, kind, body):
            pytest.fail("qview без сессии канала конфигурации")
    monkeypatch.setattr(linkclient, "ensure", lambda services: _Lc())
    await guestlink.run_actions(types.SimpleNamespace(), [("link", "qview", {"l": "a"})])
