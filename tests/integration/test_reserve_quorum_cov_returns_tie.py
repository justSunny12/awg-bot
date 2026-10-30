"""Возвраты, ничья, такт и карточка кворума — ветви, которых нет в
test_reserve_quorum_returns / _tie / _votes (domain/services/reserve/quorum/returns.py,
tie.py, tick.py, screen.py): отказ аварийного возврата и повтор, возврат при уже
идущем ходе, ничья о своём клоне без тревоги, эпизод ничьей после перезапуска
(без повтора тревоги, но с отбоем), устойчивость такта к отказу шага, `qkeys`
тактом, план сокетов (привязка ответчика к awg0), строки карточки: молчащий по
давности голосующий, голоса о боте на резерве в отъезде, кривой состав второй
линии, кривое имя узла, отказ корреляции.

Миры — из соседних тестов: основа в отъезде (`_Away`, test_reserve_quorum_returns),
кандидат и лидер ничьей (`_Cand`, `_Lead`, test_reserve_quorum_tie), линия «Alpha»
с настоящими ключами и зондами (`quo`, tests/integration/_reserve_quorum_common.py).

Цена ошибки: отказ аварийного возврата роняет такт или больше не повторяется —
мёртвый резерв держит пользователей основы до ручного рычага; тревога ничьей о
своём клоне — громкое сообщение о споре, в котором бот сам ничего не решит и
которого нет в текстах; после перезапуска тревога ничьей повторяется, а отбой
не приходит — админы не знают, кончилось ли; отказ шага ключей роняет весь такт
кворума — ни проб, ни счёта; ответчик без привязки к awg0 — проба с eth0 в счёте.
"""
from __future__ import annotations

import json

import pytest

from awgbot.core import config
from awgbot.domain.services.types import ServiceError

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration import _reserve_quorum as rq
from tests.integration import _reserve_quorum_common as qc
from tests.integration._reserve_vote_common import box, notes
from tests.integration.test_reserve_quorum_returns import T_UP, _Away, gwlink
from tests.integration.test_reserve_quorum_tie import T_TIE, _Cand as _TieCand, _Lead as _TieLead

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
quo = qc.quo
__all__ = ["box"]

CLEAR = 60


# ── аварийный возврат ───────────────────────────────────────────────────────

@pytest.fixture()
def aw(mv, monkeypatch):
    return _Away(mv, monkeypatch)


def _no_clone(aw) -> list[tuple]:
    aw.clone.view = "uns"
    aw.gw_view(1, "uns")
    aw.gw_view(2, "uns")
    return aw.tick()


def test_a_refused_emergency_return_does_not_break_the_tick_and_is_tried_again(aw, monkeypatch):
    tried = []

    def refuse(check=True):
        tried.append(check)
        raise ServiceError("идёт ход")
    monkeypatch.setattr(aw.s, "reserve_force_home", refuse)
    _no_clone(aw)
    acts = aw.run(T_UP + 10)
    assert tried == [False], f"аварийный возврат не попробован (или с проверкой С9): {tried}"
    assert aw.s.reserve_out() == "away"
    assert ("take_home",) not in acts
    aw.run(T_UP + 10)
    assert len(tried) == 2, "после отказа аварийный возврат больше не пробуется — мёртвый резерв держит пользователей"


def test_an_emergency_return_while_a_return_is_already_going_does_nothing(aw, monkeypatch):
    monkeypatch.setattr(aw.s, "reserve_out", lambda: "returning")
    called = []
    monkeypatch.setattr(aw.s, "reserve_force_home", lambda check=True: called.append(1) or [])
    _no_clone(aw)
    acts = aw.run(T_UP + 10)
    assert called == [] and not gwlink(acts, "qtake"), "аварийный возврат поверх идущего возврата"


def test_a_tie_about_my_own_clone_raises_no_alarm_and_no_return(mv, monkeypatch):
    """v = 4 о боте на резерве (я, a1, a2, a3): двое видят, двое нет. Тревоги ничьей о
    своём клоне в текстах нет, и решать её боту нечем: только строка журнала."""
    aw = _Away(mv, monkeypatch)
    members = ["a", "b", "a1", "a2", "a3"]
    aw.s.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": members},
                    mine={"l": "a", "g": 1, "members": members})
    aw.sessions[3] = True
    aw.clone.view = "uns"
    aw.gw_view(1, "uns")
    aw.gw_view(2, "see")
    aw.gw_view(3, "see")
    acts = aw.tick() + aw.run(T_TIE + 30)
    assert [a for a in acts if a[0] == "notify"] == [], f"ничья о своём клоне дала уведомление: {acts}"
    assert not gwlink(acts, "qtake") and ("take_home",) not in acts


def test_the_card_while_away_shows_how_many_see_the_bot_at_the_reserve(aw):
    from awgbot.bot.texts.reserve.section import quorum_mine_lines
    aw.clone.view = "see"
    aw.gw_view(1, "see")
    aw.gw_view(2, "uns")
    aw.tick()
    card = aw.s.reserve_quorum_card()
    mine = card["mine"]
    assert (mine["y"], mine["v"], mine["leader_here"], mine.get("back")) == (2, 3, False, True), mine
    lines = quorum_mine_lines({**card, "on": True})
    assert any("Бот на «Beta» видят 2 из 3 — пользователи вернутся сюда сами" in ln for ln in lines), lines


# ── ничья: эпизод после перезапуска ─────────────────────────────────────────

@pytest.fixture()
def tc(box, monkeypatch):
    return _TieCand(box, monkeypatch)


def test_after_a_restart_the_candidate_does_not_repeat_the_tie_alarm_but_sends_the_all_clear(tc):
    tc.tie()
    first = tc.run(T_TIE + 5)
    assert [n for n in notes(first) if "Ничья" in n.text], "подготовка: тревоги ничьей нет"
    tc.s.__dict__.pop("_rsv_qcand", None)                    # перезапуск: машина кандидата заново, отметка — на диске
    again = tc.tie() + tc.run(T_TIE + 30)
    assert not [n for n in notes(again) if "Ничья о" in n.text], "после перезапуска тревога того же эпизода повторилась"
    tc.untie()
    over = tc.run(CLEAR + 10)
    assert [n for n in notes(over) if "Ничьей" in n.text], "после перезапуска отбой ничьей не пришёл"


@pytest.fixture()
def tl(mv, monkeypatch):
    w = _TieLead(mv.svc, monkeypatch)
    w.start()
    return w


def test_after_a_restart_the_leader_does_not_repeat_its_tie_alarm_but_sends_the_all_clear(tl):
    acts = tl.tie() + tl.run(T_TIE + 10)
    assert [n for n in notes(acts) if "Ничья" in n.text], "подготовка: Т-К5 нет"
    tl.s.__dict__.pop("_quo_ltie", None)
    again = tl.run(T_TIE + 30)
    assert not [n for n in notes(again) if "Ничья" in n.text], "после перезапуска Т-К5 того же эпизода повторился"
    tl.gw_view(2, "see")
    over = tl.run(CLEAR + 15)
    assert [n for n in notes(over) if "Ничьей" in n.text], "после перезапуска отбой Т-К6 не пришёл"


def test_a_tie_mark_of_the_other_role_does_not_silence_a_new_tie(tc):
    """Отметка ничьей лидера на диске (`side = lead`) — другой эпизод: тревога кандидата
    о второй линии приходит."""
    tc.s.rsv_update("rsv_qlog", tie={"episode": "2026-10-30T10:00:00+03:00", "told": True, "side": "lead", "l": "a"})
    tc.tie()
    acts = tc.run(T_TIE + 5)
    assert [n for n in notes(acts) if "Ничья" in n.text], "чужая отметка ничьей заглушила тревогу"


# ── такт и план ─────────────────────────────────────────────────────────────

@pytest.fixture()
def lead(quo, monkeypatch):
    from awgbot.infra import awg
    m = qc.enable(quo)
    quo.reserve_probes_refresh(m)
    quo.reserve_probe_peers_sync(m)
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: awg.parse_dump("PRIV\tPUB\t43125\toff\n"))
    return quo


@pytest.mark.parametrize("step", ["reserve_quorum_keys_sync", "reserve_probes_refresh", "reserve_probe_peers_sync",
                                  "reserve_probes_apply"])
def test_a_failing_step_does_not_stop_the_rest_of_the_tick(quo, monkeypatch, step):
    def boom(*a, **k):
        raise RuntimeError("отказ шага")
    monkeypatch.setattr(quo, step, boom)
    ran = []
    monkeypatch.setattr(quo, "reserve_quorum_leader_tick", lambda now=None: ran.append("leader") or [])
    monkeypatch.setattr(quo, "reserve_quorum_candidate_tick", lambda now=None: ran.append("cand") or [])
    quo.quorum_tick()
    assert ran == ["leader", "cand"], f"отказ шага {step} оборвал такт: {ran}"


def test_the_tick_sends_unconfirmed_qkeys_to_a_live_peer(quo):
    if not quo.reserve_keys().get("m"):
        quo.reserve_keys_init()
    quo.reserve_quorum_refresh()
    rq.ack_members(quo)
    qc.gateways_applied(quo)
    acts = quo.quorum_tick()
    assert [a for a in acts if a[:2] == ("send", "qkeys")], f"неподтверждённые ключи не ушли тактом: {[a[:2] for a in acts]}"


def test_the_responder_of_the_primary_is_bound_to_its_awg_interface(lead):
    plan = lead.quorum_runtime_plan()
    assert plan["responder"] is not None, "подготовка: ответчика нет"
    assert plan.get("responder_if") == config.AWG_INTERFACE, f"ответчик основы не привязан к интерфейсу AWG: {plan}"


def test_the_guest_plan_has_only_the_clone_responder(services, monkeypatch):
    monkeypatch.setattr(config, "GUEST", True)
    services.reserve_quorum_guest_in({"l": "a", "g": 1, "members": ["a", "b", "a1"], "port": 8789})
    plan = services.quorum_runtime_plan()
    assert plan["probes"] == {} and "responder_if" not in plan, plan
    assert plan["responder"] == ("169.254.88.2", 8789), plan


# ── карточка ────────────────────────────────────────────────────────────────

def test_a_voter_whose_probes_aged_out_reads_as_silent(lead, monkeypatch):
    from awgbot.bot.texts.reserve.section import quorum_mine_lines
    from awgbot.domain.services.reserve.quorum import leader as leader_mod
    from awgbot.util.quorum.probe import Probe
    lead.clock.t += 1
    lead.reserve_quorum_probe_in(Probe("a", 1, "b", 0, "see", 1).encode(), "169.254.88.4")
    lead.clock.t += 41
    assert leader_mod.time.monotonic() == lead.clock.t
    row = {v["node"]: v for v in lead.reserve_quorum_card()["mine"]["voters"]}["b"]
    assert row["silent"] and row["age"] > 40, row
    lines = quorum_mine_lines(lead.reserve_quorum_card())
    assert any("«Beta» — молчит" in ln for ln in lines), lines


def test_a_malformed_line_of_the_peer_shows_zero_voters(lead):
    lead.rsv_update("rsv_peer_state", quo={"l": "b", "g": 1, "members": ["b", "x9"], "active": True, "why": []})
    peer = lead.reserve_quorum_card()["peer"]
    assert peer["v"] == 0 and peer["active"], peer


def test_an_unparsable_node_is_named_as_it_came(lead):
    assert lead._quorum_node_name("zz", "a") == "zz"
    assert lead._quorum_node_name("b3", "b") == "шлюз 3 «Beta»"


def test_a_failing_correlation_gives_no_line_and_no_error(lead, monkeypatch):
    def boom():
        raise OSError("линк не читается")
    monkeypatch.setattr(lead, "reserve_quorum_correlation", boom)
    lead.__dict__.pop("_quo_corr", None)
    assert lead.reserve_quorum_card()["mine"]["correlation"] == []


def test_the_card_of_an_unpaired_server_is_empty(quo):
    quo.db.reserve_peer_delete()
    assert quo.reserve_quorum_card() == {}


def test_the_qcount_of_the_guest_reaches_the_card_when_not_leading_here(lead, monkeypatch):
    monkeypatch.setattr(lead, "reserve_quorum_leader_host", lambda: "b")
    lead.reserve_quorum_qcount_in({"y": 2, "v": 3, "r": "lead"})
    lead.__dict__.pop("_rsv_qleader", None)
    mine = lead.reserve_quorum_card()["mine"]
    assert mine.get("from_guest") and mine["y"] == 2 and mine["role"] == "lead", mine
    assert json.dumps(mine, ensure_ascii=False)
