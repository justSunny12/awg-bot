"""Плановая пауза лидера кворума (domain/services/reserve/quorum/fence.py,
runtime/rsvactions.py, bot/sections/svc.py, domain/fleet/server.py;
концепт «кворум» §3.7.12): перед перезапуском бота из «🔧 Сервис» и перед
ступенью обновления сервера лидер шлёт `qpause{l, until}` (≤ 5 мин) второму
серверу — по каналу пары, если тот знает `quo1` и на связи, — и каждому шлюзу
своего состава по каналу конфигурации (действие `gwlink`).

Мир — основа «Alpha» в паре с «Beta» (tests/integration/_reserve_common.py),
линия `a` с составом a, b, a1, a2; включённость кворума — подмена предиката
`reserve_quorum_active` (её собирают тесты этапа ключей). Каналы — подставные
серверы с журналом отправленного.

Цена ошибки: без паузы плановый перезапуск бота (и обновление) выглядит
для голосующих как смерть основы — через T_no они говорят «не вижу», и
кандидат переводит всех пользователей на второй сервер ради минуты простоя;
пауза, посланная без кворума, — лишние слова старым агентам; пауза
сервера, не дошедшая из синхронного шага обновления, — тот же переезд.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from awgbot.runtime import linkserver, pairlink, rsvactions
from awgbot.util import timeutil

from tests.conftest import FakeCallback, FakeMessage
from tests.integration import _reserve_common as rc

pytestmark = pytest.mark.integration
rsv = rc.rsv

MEMBERS = ["a", "b", "a1", "a2"]


@pytest.fixture()
def lead(services, rsv, monkeypatch):
    rc.pair_row(services)
    st = types.SimpleNamespace(active=True, online=True)
    monkeypatch.setattr(services, "reserve_quorum_active", lambda: st.active)
    monkeypatch.setattr(services, "reserve_online", lambda: st.online)
    services.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": list(MEMBERS)})
    services.rsv_update("rsv_peer_state", caps=["quo1"])
    st.s = services
    return st


def _until_s(body: dict) -> float:
    return timeutil.elapsed(timeutil.now(), timeutil.parse_iso(body["until"]))


def test_the_pause_goes_to_the_second_server_and_every_gateway_of_the_line(lead):
    acts = lead.s.reserve_quorum_pause_actions()
    sends = [a for a in acts if a[0] == "send"]
    links = [a for a in acts if a[0] == "gwlink"]
    assert [a[1] for a in sends] == ["qpause"], "второму серверу пауза не ушла"
    assert sorted(a[1] for a in links) == [1, 2] and all(a[2] == "qpause" for a in links), links
    body = sends[0][2]
    assert body["l"] == "a" and all(a[3] == body for a in links)
    assert 295 <= _until_s(body) <= 300, f"срок паузы не ≈ 5 минут: {body['until']}"


def test_the_pause_is_capped_by_the_caller(lead):
    body = lead.s.reserve_quorum_pause_actions(60)[0][2]
    assert 55 <= _until_s(body) <= 60


def test_without_the_quorum_there_is_no_pause(lead):
    lead.active = False
    assert lead.s.reserve_quorum_pause_actions() == [], "пауза без включённого кворума"


def test_without_a_line_there_is_no_pause(lead):
    lead.s.rsv_update("rsv_quorum", active=None)
    assert lead.s.reserve_quorum_pause_actions() == []


def test_the_second_server_offline_or_without_quo1_gets_nothing_but_gateways_do(lead):
    lead.online = False
    acts = lead.s.reserve_quorum_pause_actions()
    assert not [a for a in acts if a[0] == "send"] and len([a for a in acts if a[0] == "gwlink"]) == 2, acts
    lead.online = True
    lead.s.rsv_update("rsv_peer_state", caps=["rsv1"])
    acts = lead.s.reserve_quorum_pause_actions()
    assert not [a for a in acts if a[0] == "send"], "qpause второму серверу без quo1"


# ── исполнитель ─────────────────────────────────────────────────────────────

class _Link:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[tuple] = []

    async def send(self, slot, kind, body=None):
        self.sent.append((slot, kind, body))
        return self.ok


async def test_gwlink_goes_over_the_config_channel(monkeypatch):
    link = _Link()
    monkeypatch.setattr(linkserver, "_server", link)
    await rsvactions.run([("gwlink", 2, "qpause", {"l": "a", "until": "x"})])
    assert link.sent == [(2, "qpause", {"l": "a", "until": "x"})], link.sent


async def test_gwlink_without_a_session_does_not_break_the_rest(monkeypatch):
    monkeypatch.setattr(linkserver, "_server", None)
    sent: list = []

    async def psend(kind, body=None):
        sent.append(kind)
        return True
    monkeypatch.setattr(pairlink, "send", psend)
    await rsvactions.run([("gwlink", 1, "qpause", {}), ("send", "qpause", {})])
    assert sent == ["qpause"], "действие после неотправленного gwlink потерялось"


async def test_run_threadsafe_puts_the_actions_on_the_bot_loop(monkeypatch):
    link = _Link()
    monkeypatch.setattr(linkserver, "_server", link)
    monkeypatch.setitem(rsvactions._ctx, "loop", asyncio.get_running_loop())
    await asyncio.to_thread(rsvactions.run_threadsafe, [("gwlink", 1, "qpause", {"l": "a"})])
    for _ in range(50):
        if link.sent:
            break
        await asyncio.sleep(0.01)
    assert link.sent == [(1, "qpause", {"l": "a"})], "действие из синхронного кода не дошло до цикла бота"


def test_run_threadsafe_without_a_loop_is_silent(monkeypatch):
    monkeypatch.setitem(rsvactions._ctx, "loop", None)
    rsvactions.run_threadsafe([("gwlink", 1, "qpause", {})])          # не падает


# ── кто зовёт ───────────────────────────────────────────────────────────────

async def test_the_bot_restart_from_service_pauses_the_line_before_the_restart(lead, monkeypatch, fake_bot):
    from awgbot.bot.sections import svc
    order: list = []

    async def run(actions):
        order.append(("run", [a[:3] for a in actions or []]))
    monkeypatch.setattr(rsvactions, "run", run)
    monkeypatch.setattr(lead.s, "restart_bot", lambda: order.append("restart"))
    monkeypatch.setattr(lead.s, "set_restart_wait", lambda *a: None)
    nav = FakeMessage("…", chat_id=1, user_id=1, bot=fake_bot)
    ctx = types.SimpleNamespace(cb=FakeCallback(message=nav, user_id=1, bot=fake_bot), services=lead.s)
    await svc._bot_restart(ctx)
    assert len(order) == 2 and order[-1] == "restart", order
    assert order[0][0] == "run" and [a[:2] for a in order[0][1]] == [("send", "qpause"), ("gwlink", 1), ("gwlink", 2)], \
        f"перед перезапуском бота паузы нет: {order}"


def test_the_server_update_step_pauses_the_line_before_applying(lead, monkeypatch):
    from awgbot.domain.fleet.order import FleetOut
    order: list = []
    monkeypatch.setattr(rsvactions, "run_threadsafe", lambda acts: order.append(("pause", [a[:2] for a in acts])))
    monkeypatch.setattr(lead.s, "apply_update", lambda step, hold="": order.append("apply"))
    out = FleetOut()
    out.server = "apply"
    out.step = types.SimpleNamespace(tag="v4.2.0")
    lead.s.fleet_server_act(out)
    assert order == [("pause", [("send", "qpause"), ("gwlink", 1), ("gwlink", 2)]), "apply"], \
        f"ступень обновления сервера без паузы лидера (или после неё): {order}"


# ── второй сервер как голосующий ────────────────────────────────────────────

def test_qpause_of_the_peer_line_holds_my_uns_until_the_term(lead):
    """Пауза «Beta» о её линии: мой зонд к её AWG не говорит «не вижу» до срока."""
    from awgbot.util.quorum.voter import Voter
    import time
    v = Voter("b", "a")
    t0 = time.monotonic()
    v.start(t0)
    lead.s._qvoters["awgqbh"] = v
    until = timeutil.to_iso(timeutil.shift(timeutil.now(), 300))
    assert lead.s.reserve_quorum_pair_in("qpause", {"l": "b", "until": until}) == []
    assert "uns" not in v.tick(t0 + 200), "«не вижу» в паузе лидера"
    assert "uns" in v.tick(t0 + 310), "после срока паузы «не вижу» так и не наступило"
