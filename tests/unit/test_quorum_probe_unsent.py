"""Проба, не ушедшая из сокета зонда, — слепота голосующего, а не молчание лидера
(util/quorum/voter.py `unsent`, runtime/quorum.py, domain/gateway/reserve/quorum.py).

Интерфейс зонда и пространство `awg-quo` поднимает такт домена, а сокет зонда
runtime пробует создать раньше этого такта: на свежем зонде (пара, новый конфиг
`qprobe`, старт после перезагрузки) первая попытка всегда отказывает, повтор —
через 30 с. Всё это время правило голосующего выдавало пробы в пустоту и считало
`no_s` от своего создания: через 45 с — «не вижу» о живом сервере, хотя ни одна
проба до него не дошла (стенд, s12_relay_flush: сокет открыт за 15 с до срока,
«не вижу», после возврата кнопкой — окно аварийного кода при живой основе).

Цена ошибки: ложное «не вижу» у резерва — окно аварийного кода и Т-К21 обоим
админам при живой основе; у шлюза — `qview{uns}` кандидату, лишний голос «нет»
к переезду при живом лидере. Обратная цена: «не ушла», засчитанная как ответ, —
мёртвая основа «видна», пока сокет не встанет; поэтому слепота только не идёт в
`no_s`, но и «вижу» не даёт.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from awgbot.domain.gateway.reserve import quorum as qmod
from awgbot.infra import quoprobe
from awgbot.runtime import quorum as rq
from awgbot.util import netns
from awgbot.util.quorum.probe import Probe, Reply
from awgbot.util.quorum.timing import Timing
from awgbot.util.quorum.voter import Voter

pytestmark = pytest.mark.unit

T = Timing()
T0 = 1000.0


def _reply(s: int) -> Reply:
    return Reply("a", 4, 12, "lead", s, 2, 3)


def _run(v: Voter, start: float, end: float, *, sent_from: float, answer_from: float | None) -> list[tuple[float, str]]:
    """Такты по 1 с: проба до `sent_from` не уходит (unsent), с `answer_from` — отвечена."""
    out: list[tuple[float, str]] = []
    now = start
    while now < end:
        for a in v.tick(now):
            if a == "probe":
                s = v.probe_out(now, T.probe_s)
                if now < sent_from:
                    v.unsent(s, now)
                elif answer_from is not None and now >= answer_from:
                    out += [(now, x) for x in v.reply_in(_reply(s), now + 0.2)]
            else:
                out.append((now, a))
        now += 1
    return out


# ── правило голосующего ─────────────────────────────────────────────────────

def test_time_without_a_probe_socket_is_not_silence_of_the_leader():
    """Сокета нет 35 с, затем пробы уходят и отвечены не сразу (первая — без ответа):
    «не вижу» нет, «вижу» — по двум ответам."""
    v = Voter(l="a", me="b", timing=T)
    v.start(T0)
    acts = _run(v, T0, T0 + 120, sent_from=T0 + 35, answer_from=T0 + 50)
    assert ("uns" not in [a for _, a in acts]), f"«не вижу» о сервере, до которого проба не доходила: {acts}"
    assert v.view == "see", f"после ответов взгляд {v.view}"


def test_a_probe_that_did_not_leave_is_not_a_lost_reply():
    v = Voter(l="a", me="b", timing=T)
    v.start(T0)
    for k in range(2):
        s = v.probe_out(T0 + 15 * k, T.probe_s)
        v.reply_in(_reply(s), T0 + 15 * k + 0.1)
    assert v.view == "see"
    s = v.probe_out(T0 + 30, T.probe_s)
    v.unsent(s, T0 + 30)
    assert v.tick(T0 + 34) == [] and v.view == "see", "неушедшая проба — пропуск ответа (sus, досылки)"


def test_after_the_blindness_a_dead_leader_is_uns_no_s_after_the_last_unsent_probe():
    """Честный путь не теряется: сокет встал, ответов нет — «не вижу» через no_s от
    последней неушедшей пробы, не раньше и не позже такта."""
    v = Voter(l="a", me="b", timing=T)
    v.start(T0)
    acts = _run(v, T0, T0 + 200, sent_from=T0 + 35, answer_from=None)
    blind = v.blind_at
    assert T0 < blind < T0 + 35
    uns = [t for t, a in acts if a == "uns"]
    assert uns and uns[0] == pytest.approx(blind + T.no_s, abs=1), f"«не вижу» в {uns}, последняя неушедшая — {blind}"


def test_permanent_blindness_never_says_uns():
    """Свой сокет так и не встал — сказать о второй стороне нечего: «не вижу» нет."""
    v = Voter(l="a", me="b", timing=T)
    v.start(T0)
    acts = _run(v, T0, T0 + 600, sent_from=T0 + 10_000, answer_from=None)
    assert not [a for _, a in acts if a in ("uns", "sus")] and v.view == "new", acts


def test_unsent_of_an_unknown_probe_changes_nothing():
    v = Voter(l="a", me="b", timing=T)
    v.start(T0)
    v.unsent(99, T0 + 30)
    assert v.blind_at == 0.0 and v.tick(T0 + T.no_s) == ["uns", "probe"]


# ── runtime: о неушедшей пробе говорит домену ───────────────────────────────

class _OldSvc:
    """Домен прежнего контракта: без `quorum_probe_unsent`."""

    def __init__(self):
        self.plan = {"responder": None, "probes": {}}
        self.tick_actions: list[tuple] = []
        self.unsent: list[tuple[str, bytes]] = []

    def quorum_runtime_plan(self):
        return self.plan

    def quorum_tick(self):
        out, self.tick_actions = self.tick_actions, []
        return out

    def quorum_probe_in(self, data, src):
        return None

    def quorum_reply_in(self, name, data):
        return []


class _Svc(_OldSvc):
    def quorum_probe_unsent(self, name, data):
        self.unsent.append((name, data))


async def test_a_probe_without_its_socket_is_reported_unsent(monkeypatch):
    def listen_in(path, make):
        raise netns.NetnsError("пространства ещё нет")
    monkeypatch.setattr(netns, "listen_in", listen_in)
    monkeypatch.setattr(quoprobe, "ns_path", lambda: "/run/netns/awg-quo")
    svc = _Svc()
    svc.plan = {"responder": None, "probes": {"awgqah": {"node_addr": "127.0.0.1", "resp": "127.0.0.1", "port": 9}}}
    svc.tick_actions = [("probe", "awgqah", b"P1", ("127.0.0.1", 9))]
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    assert svc.unsent == [("awgqah", b"P1")], "проба без сокета ушла молча — голосующий считает её молчанием лидера"
    await rt.stop()


async def test_a_probe_that_left_its_socket_is_not_reported(monkeypatch):
    monkeypatch.setattr(netns, "listen_in", lambda path, make: make())
    monkeypatch.setattr(quoprobe, "ns_path", lambda: "/run/netns/awg-quo")
    svc = _Svc()
    svc.plan = {"responder": None, "probes": {"awgqah": {"node_addr": "127.0.0.1", "resp": "127.0.0.1", "port": 9}}}
    rt = rq.QuorumRuntime(svc, lambda a: asyncio.sleep(0))
    await rt.tick()
    svc.tick_actions = [("probe", "awgqah", b"P2", ("127.0.0.1", 9))]
    await rt.tick()
    assert svc.unsent == [], f"ушедшая проба отмечена неушедшей: {svc.unsent}"
    await rt.stop()


async def test_a_domain_without_the_hook_still_works():
    """Домен без `quorum_probe_unsent` (прежний контракт) — проба без сокета тихо прочь."""
    svc = _OldSvc()
    svc.tick_actions = [("probe", "awgqzz", b"P", ("127.0.0.1", 9))]
    acts: list = []

    async def act(a):
        acts.extend(a)
    rt = rq.QuorumRuntime(svc, act)
    await rt.tick()
    assert acts == []
    await rt.stop()


# ── агент шлюза: та же слепота — не qview{uns} ──────────────────────────────

def test_the_gateway_voter_does_not_tell_uns_while_its_probes_do_not_leave(tmp_path, monkeypatch):
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    from tests.unit.test_gw_quorum import _Clock, _Host, _sec
    _Host(monkeypatch)
    c = _Clock()
    monkeypatch.setattr(qmod, "time", types.SimpleNamespace(monotonic=c.monotonic))
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    gw = GatewayServices(d)
    gw._RSV_STORE = tmp_path / "reserve.json"
    gw._QUO_STORE = tmp_path / "quorum.json"
    (tmp_path / "reserve.json").write_text(json.dumps({"slot": 1, "name": "Alpha", "state": "idle"}), encoding="utf-8")
    gw.quorum_apply(_sec())
    told = []
    for _ in range(40):                                   # 200 с без сокета зонда
        for a in gw.quorum_tick(c.t):
            if a[0] == "probe":
                gw.quorum_probe_unsent(a[1], a[2])
            elif a[:2] == ("guest", "qview"):
                told.append(a[2]["w"])
        c.t += 5
    assert told == [], f"шлюз сказал резерву о сервере, до которого его проба не доходила: {told}"
    p = None
    for _ in range(30):                                   # сокет встал, сервер молчит — честное «не вижу»
        for a in gw.quorum_tick(c.t):
            if a[0] == "probe":
                p = Probe.parse(a[2])
            elif a[:2] == ("guest", "qview"):
                told.append(a[2]["w"])
        c.t += 5
    assert p is not None and told == ["uns"], f"после слепоты молчание сервера не стало «не вижу»: {told}"
