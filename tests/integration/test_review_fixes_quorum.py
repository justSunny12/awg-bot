"""Правки по ревью правил кворума резерва (domain/services/reserve/quorum/candidate.py,
gwguest.py, inbound.py, leader.py, tie.py, codewin.py; util/quorum/voter.py,
candidate.py; util/pairproto.py; runtime/pairlink/inbound.py; runtime/scheduler/reserve.py):

- эпоха `qtake`, `state{e}` и `qstate{e}` шлюзам — по фактам (`Voter.fact`): поднятая
  собой после «забираю» (`raise_to(e + 1)`) остаётся только у своей пробы;
- взятие, которому мешают одни паузы автоматического переезда (демпфер после срыва,
  интервал между автоматическими переездами), при своём «не вижу» открывает окно кода
  сразу и исполняет то же решение, когда пауза кончилась;
- вид переезда Р6 (`dead` | `rf`) едет в `ask{q}` и в открытие по точкам;
- гостю — включена ли линия основы (`active`): выключенная не ограждается по голосам;
- ничья кандидата и лидера — разные записи (`tie_cand`, `tie_lead`);
- при v = 1 кандидат не держит «решено»;
- живость канала шлюза, закрытие окна кода с `by`, `qprobe` без канала пары.

Миры — из соседних тестов: кандидат `Cand` поверх `box` (tests/integration/_reserve_vote_common.py),
кандидат и лидер ничьей (`_Cand`, `_Lead` из test_reserve_quorum_tie), линия «Alpha»
с зондами (`quo`, tests/integration/_reserve_quorum_common.py). Хост и сеть не трогаются.

Цена ошибки: повторный `qtake` после срыва открытия под e + 1 — шлюзы уходят вперёд
эпохи клона (эпоха пакета + 1), клон навсегда без права записи; пауза после срыва
отказывает `qrel` вместо окна кода — админ основы ждёт аварийного кода до следующего
эпизода, а по концу паузы решение уже никто не исполняет; вид `dead` в случае 3 —
тексты врут, что сервер не отвечает никому; гость выключенной линии ограждается по
голосам, которых никто не шлёт, — бот на резерве теряет право записи; ничья лидера
затирает ничью кандидата — карточка и отбой о второй линии теряются; «решено» при
v = 1 переживает переход на v = 2 — «забираю» не отдаётся; `qprobe`, отброшенный без
канала пары, считается отправленным — зонд второй стороны не встаёт до смены поколения.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config, settings
from awgbot.util import pairproto, timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration import _reserve_quorum_common as qc
from tests.integration._reserve_vote_common import (ASK, RF, T_UP, Cand as _Cand, box, gw_sent, notes, sent)
from tests.integration.test_reserve_quorum_tie import T_TIE, _Cand as _TieCand, _Lead as _TieLead

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
quo = qc.quo
__all__ = ["box"]

DAMPER = 120                                    # FAIL_DAMPER_S


@pytest.fixture()
def c(box, monkeypatch):
    return _Cand(box, monkeypatch)


def _majority(c) -> list[tuple]:
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "uns")
    return c.tick()


def _taken(c) -> list[tuple]:
    return _majority(c) + c.run(T_UP)


def _qtake_epochs(acts) -> list[int]:
    return [int(b["e"]) for _, b in gw_sent(acts, "qtake")]


def _failed_open(c, monkeypatch) -> None:
    """Решение дошло до процесса открытия по точкам, и процесс сорвался (`_take_fail`):
    зона снята, направление «к себе» снова свободно, отметка срыва — сейчас."""
    from awgbot.infra import rsvzone
    monkeypatch.setattr(rsvzone, "teardown", lambda: "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: "")
    monkeypatch.setattr(c.s, "_rsvopen_stop", lambda: None)
    _taken(c)
    c.run(ASK)
    assert c.stage() == "points", f"подготовка: решение не дошло до точек: {c.stage()!r}"
    c.share(1)
    assert c.b.launched and c.s.reserve_in() == "taking", "подготовка: процесс открытия не запущен"
    c.tick()                                              # открытие пошло — исполнение кандидата снято
    c.s._take_fail("процесс открытия отказал")
    assert c.s.reserve_in() == "idle" and c.s.rsv_get("rsv_in").get("fail_at"), "подготовка: срыв не записан"


# ── эпоха по фактам (quorum-1) ──────────────────────────────────────────────

def test_after_the_take_the_gateways_get_the_epoch_of_the_facts_not_the_raised_one(c):
    """После «забираю» своя проба идёт под e + 1, но `state{e}` и `qstate{e}` шлюзам —
    эпоха линии по фактам (7): поднятая собой не факт, иначе шлюзы уходят вперёд клона."""
    _taken(c)
    assert c.v.epoch == 8, "подготовка: своя проба после «забираю» не под e + 1"
    assert c.s._state_body().get("e") == 7, f"state шлюзам несёт поднятую собой эпоху: {c.s._state_body()}"
    qs = c.s._quorum_qstate_body()
    assert qs is not None and qs["e"] == 7, f"qstate шлюзам несёт поднятую собой эпоху: {qs}"


def test_a_new_take_after_the_execution_was_dropped_goes_under_the_same_epoch(c):
    """Решение снято своим «вижу» (случай 3 своим длинным таймером): новое «забираю»
    идёт под той же эпохой 7 — исход прежнего так и не случился."""
    first = _qtake_epochs(_taken(c))
    assert first and set(first) == {7}, f"подготовка: первое «забираю» не под 7: {first}"
    c.own("see")
    c.tick()
    assert c.stage() == "", "подготовка: исполнение не снято своим «вижу»"
    again = _qtake_epochs(c.run(RF + 20))
    assert again, "подготовка: случай 3 не забран через T_up_rf"
    assert set(again) == {7}, f"повторное «забираю» под поднятой собой эпохой: {again}"


def test_a_retake_after_a_failed_opening_goes_under_the_same_epoch(c, monkeypatch):
    """Открытие по точкам сорвалось: следующее «забираю» (после паузы демпфера) — под той
    же эпохой 7, а не 8: клон ещё не стал писателем, и шлюзы не должны уйти вперёд."""
    _failed_open(c, monkeypatch)
    acts = c.run(T_UP + DAMPER + 10)
    epochs = _qtake_epochs(acts)
    assert epochs, f"после срыва и паузы решение не исполнено: {[a[:3] for a in acts]}"
    assert set(epochs) == {7}, f"повторное «забираю» после срыва — под эпохой {epochs}, а не 7"


# ── паузы автоматического переезда (quorum-3) ───────────────────────────────

def test_the_damper_pause_opens_the_code_window_at_once_and_sends_no_qrel(c, monkeypatch):
    """Решение созрело в паузе после срыва открытия: код от паузы не зависит — окно кода
    сразу (Т-К21), исполнение ждёт конца паузы; `qrel` не уходит — обязательства «нет»
    у шлюзов остаются для того же решения (откат сорвавшегося «забираю» — `qrel{7}` в первом
    такте после срыва — не об этом решении)."""
    _failed_open(c, monkeypatch)
    rollback = c.run(5)
    assert sorted(s for s, _ in gw_sent(rollback, "qrel")) == [1, 2], f"срыв открытия — без отката qtake: {rollback}"
    acts = c.run(T_UP)
    assert c.s.reserve_auto_block(), "подготовка: пауза демпфера уже кончилась"
    assert not c.s.reserve_open_block(), "подготовка: мешает не только пауза"
    assert c.s.reserve_code_window()["open"], "пауза после срыва — окно кода не открыто"
    ex = c.s.__dict__.get("_quo_exec") or {}
    assert ex.get("stage") == "code" and ex.get("paused") is True, f"исполнение в паузе не помечено: {ex}"
    assert not gw_sent(acts, "qrel"), f"пауза отпустила «нет» шлюзов: {gw_sent(acts, 'qrel')}"
    assert not gw_sent(acts, "qtake") and not sent(acts, "ask"), "«забираю» в паузе автоматического переезда"


def test_when_the_damper_pause_ends_the_same_decision_is_executed(c, monkeypatch):
    _failed_open(c, monkeypatch)
    c.run(T_UP + 5)
    assert c.s.reserve_code_window()["open"], "подготовка: окно кода в паузе не открыто"
    acts = c.run(DAMPER)
    assert sorted(s for s, _ in gw_sent(acts, "qtake")) == [1, 2], f"по концу паузы «забираю» не ушло: {acts}"
    asks = sent(acts, "ask")
    assert len(asks) == 1 and asks[0]["case"] == "q", f"по концу паузы просьбы лидеру нет: {asks}"
    assert c.stage() in ("ask", "points"), f"по концу паузы исполнение не пошло: {c.stage()!r}"
    assert c.s.reserve_code_window()["open"], "исполнение по концу паузы закрыло окно кода"


def test_the_interval_between_automatic_moves_opens_the_code_window_and_then_takes(c):
    """Прошлый автоматический переезд был недавно (интервал не вышел): окно кода сразу,
    по концу интервала — то же решение; без канала пары — сразу по точкам (`qneed`)."""
    settings.set_value("app.reserve.min_interval_minutes", 3)
    c.s.rsv_update("rsv_in", since=timeutil.now_iso(), ended_auto=True)
    c.b.online = False
    acts = _taken(c)
    assert "между автоматическими переездами" in c.s.reserve_auto_block(), "подготовка: интервал не мешает"
    assert c.s.reserve_code_window()["open"], "интервал между переездами — окно кода не открыто"
    assert not gw_sent(acts, "qrel") and not gw_sent(acts, "qtake"), acts
    acts = c.run(180)
    assert sorted({s for s, _ in gw_sent(acts, "qneed")}) == [1, 2], f"по концу интервала точки не попрошены: {acts}"
    assert _qtake_epochs(acts) == [7, 7], f"по концу интервала «забираю» не под эпохой 7: {acts}"


def test_a_pause_with_own_see_keeps_the_refusal_with_qrel_and_no_window(c):
    """Случай 3 (свой «вижу») в паузе: окна кода нет (код — только при своём «не вижу»),
    решение отказано прежним путём — `qrel` сказавшим «нет»."""
    c.s.rsv_update("rsv_in", since=timeutil.now_iso(), ended_auto=True)   # интервал 30 мин > T_up_rf
    c.view(1, "uns")
    c.view(2, "uns")
    acts = c.tick() + c.run(RF + 5)
    assert sorted(s for s, _ in gw_sent(acts, "qrel")) == [1, 2], f"случай 3 в паузе — без qrel: {gw_sent(acts, 'qrel')}"
    assert not c.s.reserve_code_window()["open"], "случай 3 в паузе открыл окно кода"
    assert not gw_sent(acts, "qtake") and c.stage() == ""


def test_a_real_open_block_with_own_unseen_still_refuses_with_qrel(c):
    """Готовность выключена — мешает не пауза: прежний отказ, `qrel`, исполнения в паузе нет."""
    settings.set_value("app.reserve.ready", False)
    acts = _taken(c)
    assert sorted(s for s, _ in gw_sent(acts, "qrel")) == [1, 2], "отказ предусловий — без qrel"
    assert not (c.s.__dict__.get("_quo_exec") or {}).get("paused"), "выключенная готовность сочтена паузой"
    assert not gw_sent(c.run(DAMPER + 30), "qtake"), "при выключенной готовности «забираю» ушло позже"


# ── вид переезда (quorum-4) ─────────────────────────────────────────────────

def test_case_3_asks_the_leader_with_kind_rf_and_opens_by_points_as_rf(c):
    c.view(1, "uns")
    c.view(2, "uns")
    acts = c.tick() + c.run(RF + 5)
    asks = sent(acts, "ask")
    assert len(asks) == 1 and asks[0]["kind"] == "rf", f"случай 3 — вид не rf в ask{{q}}: {asks}"
    pairproto.check(pairproto.PAIR_PROTO, "ask", dict(asks[0]), pairproto.INVITER)
    c.run(ASK)
    c.share(1)
    c.share(2)
    assert c.b.launched and c.b.launched[0]["kind"] == "rf", f"случай 3 открыт не как rf: {c.b.launched}"
    assert c.s.rsv_get("rsv_in").get("kind") == "rf", "приём записан не как rf"


def test_own_unseen_opens_by_points_as_dead(c):
    _taken(c)
    c.run(ASK)
    c.share(1)
    assert c.b.launched and c.b.launched[0]["kind"] == "dead", c.b.launched


@pytest.fixture()
def leader(services, rsv, monkeypatch):
    rc.pair_row(services)
    moves: list[dict] = []
    monkeypatch.setattr(services, "reserve_move_check", lambda: "")
    monkeypatch.setattr(services, "reserve_move_start", lambda **kw: moves.append(kw) or {})
    return services, moves


SINCE = "2026-10-30T10:05:00+03:00"


@pytest.mark.parametrize("kind, want", [("rf", "rf"), ("dead", "dead"), ("zzz", "dead"), ("", "dead")])
def test_the_leader_starts_the_move_of_the_kind_the_quorum_asked(leader, kind, want):
    svc, moves = leader
    assert svc.reserve_ask_in("q", SINCE, kind) == [("live",)]
    assert moves == [{"kind": want, "dead_since": SINCE}], f"вид {kind!r}: переезд начат не как {want}: {moves}"


def test_the_leader_without_a_kind_moves_as_dead(leader):
    svc, moves = leader
    svc.reserve_ask_in("q", SINCE)
    assert moves == [{"kind": "dead", "dead_since": SINCE}], moves


@pytest.mark.parametrize("body", [{"case": "q", "since": SINCE}, {"case": "q", "since": SINCE, "kind": "rf"}],
                         ids=["without_kind", "with_kind"])
def test_the_pair_protocol_lets_ask_through_with_and_without_kind(body):
    """Сторона 4.2.0 до правки шлёт ask без kind — словарь пары не должен рвать канал."""
    for who in (pairproto.INVITER, pairproto.ACCEPTOR):
        assert pairproto.check(pairproto.PAIR_PROTO, "ask", dict(body), who) == body
    assert pairproto.OPTIONAL["ask"] == frozenset({"kind"})


@pytest.mark.parametrize("body, want", [({"case": "q", "since": SINCE}, "dead"),
                                        ({"case": "q", "since": SINCE, "kind": "rf"}, "rf")])
async def test_the_pair_channel_hands_the_kind_of_ask_to_the_leader(body, want):
    from awgbot.runtime.pairlink.inbound import InboundMixin
    got: list[tuple] = []

    class _Stub:
        services = types.SimpleNamespace(reserve_ask_in=lambda case, since, kind: got.append((case, since, kind)) or [])

        async def _actions(self, acts):
            return None
    await InboundMixin._in_ask(_Stub(), None, dict(body))
    assert got == [("q", SINCE, want)], f"канал пары потерял вид переезда: {got}"


# ── гость: включена ли линия основы (quorum-5) ──────────────────────────────

GUEST_LINE = {"l": "a", "g": 3, "members": ["a", "b", "a1", "a2"], "port": 8789}


@pytest.fixture()
def guest(quo, monkeypatch):
    from awgbot.infra import awg
    monkeypatch.setattr(config, "GUEST", True)
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: [])
    return quo


def test_the_reserve_bot_tells_the_guest_that_the_line_is_off(quo):
    quo.rsv_update("rsv_peer_state", quo={"l": "b", "g": 2, "members": ["b", "a", "b1"], "active": False, "why": ["keys"]})
    body = quo.reserve_quorum_for_guest()
    assert body is not None and body["active"] is False, f"гостю не сказано, что линия основы выключена: {body}"


def test_a_guest_of_a_switched_off_line_has_no_leader_machine(guest):
    guest.reserve_quorum_guest_in({**GUEST_LINE, "active": False})
    assert guest._quorum_leader_setup() is None, "гость выключенной линии ограждается по голосам"
    from awgbot.util.quorum.probe import Probe
    from awgbot.util import probenet
    got = guest.reserve_quorum_probe_in(Probe("a", 3, "a", 0, "see", 1).encode(), probenet.node_addr("a", "a"))
    assert got is None, "гость выключенной линии отвечает на пробы"


def test_a_guest_of_a_switched_off_line_has_no_responder(guest):
    guest.reserve_quorum_guest_in({**GUEST_LINE, "active": False})
    assert guest.reserve_quorum_responder_addr() is None, \
        "гость выключенной линии держит ответчик: голосующие видят его живым"


@pytest.mark.parametrize("extra", [{"active": True}, {}], ids=["active", "older_reserve_without_the_field"])
def test_a_guest_of_an_active_line_or_without_the_field_has_the_leader(guest, extra):
    guest.reserve_quorum_guest_in({**GUEST_LINE, **extra})
    assert guest._quorum_leader_setup() is not None, f"гость включённой линии без лидера ({extra})"
    assert guest.reserve_quorum_responder_addr() == ("169.254.88.2", 8789)


# ── ничья: свои записи кандидата и лидера (quorum-6) ────────────────────────

LEAD_TIE = {"episode": "2026-10-30T09:00:00+03:00", "told": True, "side": "lead", "l": "a"}
CAND_TIE = {"episode": "2026-10-30T09:30:00+03:00", "told": True, "side": "cand", "l": "b"}


@pytest.fixture()
def tc(box, monkeypatch):
    return _TieCand(box, monkeypatch)


@pytest.fixture()
def tl(mv, monkeypatch):
    w = _TieLead(mv.svc, monkeypatch)
    w.start()
    return w


def test_a_candidate_tie_does_not_overwrite_the_leader_tie(tc):
    tc.s.rsv_update("rsv_qlog", tie_lead=dict(LEAD_TIE))
    tc.tie()
    acts = tc.run(T_TIE + 5)
    assert [n for n in notes(acts) if "Ничья" in n.text], "подготовка: тревоги ничьей кандидата нет"
    log = tc.s.rsv_get("rsv_qlog")
    assert log.get("tie_lead") == LEAD_TIE, f"ничья кандидата затёрла ничью лидера: {log.get('tie_lead')}"
    assert (log.get("tie_cand") or {}).get("told") is True, f"ничья кандидата не записана своей записью: {log}"
    assert tc.s.reserve_quorum_tie_view("lead") == {"since": LEAD_TIE["episode"]}
    assert tc.s.reserve_quorum_tie_view("cand") == {"since": log["tie_cand"]["episode"]}


def test_a_leader_tie_does_not_overwrite_the_candidate_tie(tl):
    tl.s.rsv_update("rsv_qlog", tie_cand=dict(CAND_TIE))
    acts = tl.tie() + tl.run(T_TIE + 10)
    assert [n for n in notes(acts) if "Ничья" in n.text], "подготовка: Т-К5 нет"
    log = tl.s.rsv_get("rsv_qlog")
    assert log.get("tie_cand") == CAND_TIE, f"ничья лидера затёрла ничью кандидата: {log.get('tie_cand')}"
    assert (log.get("tie_lead") or {}).get("told") is True, f"ничья лидера не записана своей записью: {log}"
    assert tl.s.reserve_quorum_tie_view("cand") == {"since": CAND_TIE["episode"]}
    assert tl.s.reserve_quorum_tie_view("lead") == {"since": log["tie_lead"]["episode"]}


def test_a_leader_tie_mark_does_not_silence_a_new_candidate_tie(tc):
    tc.s.rsv_update("rsv_qlog", tie_lead=dict(LEAD_TIE))
    tc.s.__dict__.pop("_rsv_qcand", None)                    # перезапуск: машина кандидата заново
    tc.tie()
    assert [n for n in notes(tc.run(T_TIE + 5)) if "Ничья" in n.text], "отметка ничьей лидера заглушила тревогу кандидата"


def test_after_a_restart_the_candidate_takes_its_tie_mark_from_its_own_record(tc):
    tc.s.rsv_update("rsv_qlog", tie_cand=dict(CAND_TIE))
    tc.s.__dict__.pop("_rsv_qcand", None)
    tc.tie()
    assert tc.s._qcand.tie_told is True, "после перезапуска отметка ничьей кандидата не восстановлена"
    assert not [n for n in notes(tc.run(T_TIE + 30)) if "Ничья о" in n.text], "тревога того же эпизода повторилась"


def test_after_a_restart_the_leader_takes_its_tie_mark_from_its_own_record(tl):
    tl.s.rsv_update("rsv_qlog", tie_lead=dict(LEAD_TIE))
    tl.s.__dict__.pop("_quo_ltie", None)
    acts = tl.tie() + tl.run(T_TIE + 30)
    assert tl.s.__dict__["_quo_ltie"]["told"] is True, "после перезапуска отметка ничьей лидера не восстановлена"
    assert not [n for n in notes(acts) if "Ничья" in n.text], "Т-К5 того же эпизода повторился после перезапуска"


# ── v = 1 не держит «решено» (quorum-7) ─────────────────────────────────────

def test_with_one_voter_the_candidate_never_holds_decided(box):
    c = _Cand(box, members=["b", "a"])
    c.own("uns")
    c.tick()
    c.run(T_UP + 30)
    assert c.s._qcand is not None and c.s._qcand.v == 1, "подготовка: голосующих не один"
    assert c.s._qcand.decided is False, "при v = 1 кандидат держит «решено» — переход на v = 2 не даст «забираю»"


# ── вне оси ─────────────────────────────────────────────────────────────────

def test_the_gateway_channel_is_alive_only_with_an_open_session_and_not_down(c):
    from awgbot.util import livestep
    assert c.s._quorum_gw_channel_alive(1) is True, "открытая сессия шлюза сочтена мёртвой"
    c.b.gone(1)
    assert c.s._quorum_gw_channel_alive(1) is False, "закрытая сессия шлюза сочтена живой"
    c.s._gwv[2]["lv"] = livestep.Liveness(state="down")
    assert c.s._quorum_gw_channel_alive(2) is False, "живость «down» сочтена живым каналом"
    assert c.s._quorum_gw_channel_alive(9) is False, "шлюз без сессии сочтён живым"


def test_closing_the_code_window_keeps_who_opened_it(c):
    c.s.reserve_code_window_set(True, since="2026-10-30T10:00:00+03:00", n=0, v=3, t=2)
    by = (c.s.rsv_get("rsv_view").get("code") or {}).get("by")
    assert by, "подготовка: окно без отметки, кто открыл"
    c.s.reserve_code_window_set(False, by="seen")
    cw = c.s.rsv_get("rsv_view").get("code") or {}
    assert cw.get("open") is False and cw.get("by") == by, f"закрытие окна потеряло by: {cw}"


class _Sched:
    def __init__(self):
        self.jobs: dict[str, tuple] = {}

    def add_job(self, fn, trigger, id, **kw):
        self.jobs[id] = (fn, trigger, kw)


@pytest.fixture()
def qjob(monkeypatch):
    """Задание живости резерва с подменённым тактом кворума: он отдаёт заданные действия;
    исполнитель действий — журнал; сервисы — заглушка с отметкой сброса `qprobe`."""
    from awgbot.runtime import pairlink, quorum as quorum_rt, rsvactions
    from awgbot.runtime.scheduler import reserve as jobs
    w = types.SimpleNamespace(acts=[], ran=[], resets=0, online=False)

    def reset():
        w.resets += 1
    svc = types.SimpleNamespace(reserve_liveness_tick=lambda: ("", []), reserve_gwguest_tick=lambda: [],
                                reserve_st_if_changed=lambda: None, reserve_qprobe_reset=reset)

    async def run(acts):
        w.ran.append(list(acts))

    async def qtick(services, act):
        await act(list(w.acts))
    monkeypatch.setattr(pairlink, "online", lambda: w.online)
    monkeypatch.setattr(rsvactions, "run", run)
    monkeypatch.setattr(quorum_rt, "tick", qtick)
    sched = _Sched()
    jobs.setup_reserve_jobs(sched, svc, None)
    w.fn = sched.jobs["rsv_liveness"][0]
    return w


QPROBE = ("send", "qprobe", {"l": "a", "g": 1, "conf": "DUMMY"})
GW = ("gw", 1, "qview", {"l": "b"})


async def test_a_qprobe_dropped_without_the_pair_link_is_not_counted_as_sent(qjob):
    qjob.acts = [QPROBE, GW]
    await qjob.fn()
    assert qjob.resets == 1, "qprobe отброшен без канала пары, а отметка «отправлено» осталась"
    assert qjob.ran[-1] == [GW], f"без канала пары слово второй стороне ушло исполнителю: {qjob.ran}"


async def test_other_dropped_words_do_not_reset_the_qprobe_mark(qjob):
    qjob.acts = [("send", "qkeys", {"l": "a"}), GW]
    await qjob.fn()
    assert qjob.resets == 0, "сброс отметки qprobe без отброшенного qprobe"


async def test_with_the_pair_link_qprobe_goes_and_the_mark_stays(qjob):
    qjob.online = True
    qjob.acts = [QPROBE]
    await qjob.fn()
    assert qjob.resets == 0 and qjob.ran[-1] == [QPROBE], f"при живом канале qprobe не ушёл: {qjob.ran}"


# ── старт лидера без права записи (стенд: leader_start) ─────────────────────

from awgbot.domain.services.reserve.quorum import fence as fence_mod, leader as leader_mod   # noqa: E402
from tests.integration.test_reserve_quorum_returns import _Away   # noqa: E402

LMEMBERS = ["a", "b", "a1", "a2"]


@pytest.fixture()
def home(mv, monkeypatch):
    """Линия «Alpha» дома включена (состав a, b, a1, a2), часы лидера модельные."""
    s = mv.svc
    t = {"v": 50_000.0}
    ns = types.SimpleNamespace(monotonic=lambda: t["v"])
    monkeypatch.setattr(leader_mod, "time", ns)
    monkeypatch.setattr(fence_mod, "time", ns)
    monkeypatch.setattr(s, "reserve_quorum_active", lambda: True)
    monkeypatch.setattr(s, "reserve_quorum_off_reasons", lambda: [])
    s.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": list(LMEMBERS)},
                 mine={"l": "a", "g": 1, "members": list(LMEMBERS)})
    return s


def test_the_leader_start_fences_at_once_before_any_quorum_tick(home):
    assert home.db.jrn_meta()["role"] == "holder", "подготовка: основа не писатель до старта"
    home.reserve_quorum_leader_start()
    fenced = dict(home.rsv_get("rsv_qlog").get("fenced") or {})
    assert fenced.get("on") is True and fenced.get("start") is True, f"старт без ограждения «старт»: {fenced}"
    assert home.db.jrn_meta()["role"] != "holder", "до первого большинства «да» основа пишет"
    assert "_quo_start_pending" not in home.__dict__, "отметка старта осталась — такт оградит второй раз"


def test_the_leader_start_twice_fences_once(home):
    home.reserve_quorum_leader_start()
    since = home.rsv_get("rsv_qlog")["fenced"]["since"]
    home.reserve_quorum_leader_start()
    assert home.rsv_get("rsv_qlog")["fenced"]["since"] == since, "повторный старт — новое ограждение"


def test_the_leader_start_with_one_voter_does_nothing(home):
    home.rsv_update("rsv_quorum", active={"l": "a", "g": 1, "members": ["a", "b"]}, mine={"l": "a", "g": 1, "members": ["a", "b"]})
    home.reserve_quorum_leader_start()
    assert not home.rsv_get("rsv_qlog").get("fenced") and home.db.jrn_meta()["role"] == "holder", \
        "при v = 1 старт оградил запись — решений по кворуму при одном голосующем нет"


def test_the_leader_start_of_the_guest_does_nothing(home, monkeypatch):
    monkeypatch.setattr(config, "GUEST", True)
    assert home.guest, "подготовка: процесс не гость"
    home.reserve_quorum_leader_start()
    assert not home.rsv_get("rsv_qlog").get("fenced"), "гость оградился как лидер основы"


def test_the_leader_start_while_away_does_nothing(home, monkeypatch):
    monkeypatch.setattr(home, "reserve_quorum_leader_host", lambda: "b")
    home.rsv_update("rsv_out", state="away")
    home.reserve_quorum_leader_start()
    assert not home.rsv_get("rsv_qlog").get("fenced"), "в отъезде (счёт Q2) старт оградил запись"


def test_the_main_process_calls_the_leader_start_before_polling():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "awgbot" / "runtime" / "main" / "client.py").read_text(encoding="utf-8")
    start = src.index("reserve_quorum_leader_start()")
    assert start < src.index("PollControl("), "ограждение старта — после поллинга: правки до первого такта пишутся"


# ── ответчик дома на шагах передачи (стенд: away_for_q2) ────────────────────

@pytest.fixture()
def aw(mv, monkeypatch):
    return _Away(mv, monkeypatch)


@pytest.mark.parametrize("state, step", [("away", ""), ("leaving", "record"), ("leaving", "release"),
                                         ("leaving", "released"), ("returning", "back"), ("returning", "")])
def test_the_home_responder_counts_for_q2_during_the_handover(aw, state, step):
    aw.s.rsv_update("rsv_out", state=state, step=step)
    assert aw.s.reserve_quorum_responder_addr() is not None, f"{state}/{step}: ответчик дома закрыт — «не вижу» шлюзов"
    ld = aw.s._quorum_leader_setup()
    assert ld is not None and ld.q2, f"{state}/{step}: ответчик дома не в роли счёта Q2"


@pytest.mark.parametrize("state, step", [("returning", "relay"), ("leaving", "ask"), ("home", "")])
def test_outside_the_handover_the_home_side_is_not_a_q2_counter(aw, state, step):
    aw.s.rsv_update("rsv_out", state=state, step=step)
    ld = aw.s._quorum_leader_setup()
    assert ld is None or not ld.q2, f"{state}/{step}: счёт Q2 вне передачи"


# ── просьба лидеру только при живой связи (стенд: peer_reachable) ───────────

def test_with_the_peer_seen_down_the_take_goes_straight_to_the_points(c, monkeypatch):
    monkeypatch.setattr(c.s, "reserve_peer_seen", lambda: "down")
    acts = _taken(c)
    assert not sent(acts, "ask"), "просьба лидеру, которого линк пары не видит, — ask_seconds впустую"
    assert sorted({s for s, _ in gw_sent(acts, "qneed")}) == [1, 2] and c.stage() == "points"


def test_with_the_peer_seen_alive_the_take_asks_the_leader(c, monkeypatch):
    monkeypatch.setattr(c.s, "reserve_peer_seen", lambda: "ok")
    acts = _taken(c)
    assert len(sent(acts, "ask")) == 1 and not gw_sent(acts, "qneed") and c.stage() == "ask"


def test_a_busy_leader_gone_silent_is_not_asked_again(c, monkeypatch):
    seen = {"v": "ok"}
    monkeypatch.setattr(c.s, "reserve_peer_seen", lambda: seen["v"])
    _taken(c)
    c.s.reserve_ask_no_in("busy")
    seen["v"] = "down"
    acts = c.run(ASK + 5)
    assert not sent(acts, "ask") and gw_sent(acts, "qneed"), "«занят» и линк пары молчит — повтор просьбы вместо точек"


# ── такт кворума гостя — своим заданием (стенд: guest_quorum) ───────────────

async def test_the_guest_quorum_runs_as_its_own_5_second_job(services, fake_bot, monkeypatch):
    from awgbot.runtime import quorum as quorum_rt
    from awgbot.runtime.scheduler import setup_guest_scheduler
    from tests.conftest import restore_settings
    ran: list[int] = []

    async def qtick(svc, act):
        ran.append(1)
    monkeypatch.setattr(quorum_rt, "tick", qtick)
    sched = setup_guest_scheduler(services, fake_bot, services.db)
    try:
        job = sched.get_job("guest_quorum")
        assert job is not None and job.trigger.interval.total_seconds() == 5, "такта кворума гостя нет или не 5 с"
        await job.func()
        assert ran == [1], "задание guest_quorum не гоняет такт кворума"
    finally:
        restore_settings()


def test_the_guest_ctl_loop_no_longer_runs_the_quorum():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "awgbot" / "runtime" / "main" / "guest.py").read_text(encoding="utf-8")
    assert "_quorum.tick" not in src, "такт кворума гостя остался в цикле канала — его задерживают вызовы Telegram"
