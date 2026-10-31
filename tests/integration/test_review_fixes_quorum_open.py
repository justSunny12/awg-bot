"""Правки по ревью исполнения решения кандидата кворума на стороне резерва
(domain/services/reserve/quorum/candidate.py, codewin.py, opening.py):

- фаза relay (пересылка хвоста прошлого возврата) — зона свободна и для исполнения:
  точки принимаются, процесс открытия идёт, окно кода открывается, паузы
  автоматического переезда (`min_interval_minutes`, демпфер) — те же, что в idle;
- открытие по точкам не пошло (предусловия к моменту точек, отказ запуска, срыв
  раньше такта) — стадия `opened` снимается, решение исполняется заново обычным путём;
- отданный `qtake` помнится до исхода: снятие исполнения без переезда (свой «вижу»
  вернулся, распад большинства, срыв открытия) — `qrel{e}` тем же узлам;
- окно кода вне решения — только при выполнимых предусловиях открытия;
- отметка автоматического переезда — по роду переезда, отказ запуска — демпфер;
- гостю причина — вид переезда (dead | rf | code), случай — отдельным полем;
- лидер с большинством «да» на `ask{q}` пользователей не отдаёт (`ask_no{quorum}`);
- цифры времени лидера едут в `st.quo`, кандидат при расхождении не включается;
- `st.out = leaving` второй стороны после просьбы — согласие: кандидат ждёт её `go_req`.

Мир — кандидат `Cand` поверх `box` (tests/integration/_reserve_vote_common.py): резерв
«Alpha» (сторона a), линия «Beta» b с составом b, a, b1, b2 (v = 3, t = 2). Хост и сеть
не трогаются.

Цена ошибки: основа умерла, пока резерв держит хвост прошлого возврата (до 2 ч тишины
или 7 дней трафика), — каждые T_up «забираю» и просьба точек впустую, точки отвергаются,
окна кода нет: пользователи без VPN, админ не знает; стадия `opened` после срыва — кандидат
немеет до распада большинства; шлюзы остаются с поднятой «забираю» эпохой e + 1 — клон
под e + 1 из пакета не станет писателем, прежний лидер под e потеряет «да»; «🆘 пришли код»,
когда код ничего не откроет; 30 мин паузы и окно кода вместо точек после сорвавшегося
запуска; гость без причины переезда и не тот текст «переподключи VPN»; одно слово по каналу
пары уводит пользователей с живой основы; два писателя по разным цифрам; переезд по
согласию лидера и по точкам параллельно — «зона занята» и срыв.
"""
from __future__ import annotations

import pytest

from awgbot.core import settings
from awgbot.infra import rsvzone
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import (ASK, NEED2, T_UP, Cand, box, gw_sent, notes)

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]

DAMPER = 120                                    # FAIL_DAMPER_S


@pytest.fixture()
def c(box, monkeypatch):
    monkeypatch.setattr(rsvzone, "teardown", lambda: "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: "")
    monkeypatch.setattr(box.s, "_rsvopen_stop", lambda: None)
    return Cand(box, monkeypatch)


def _relay(c) -> None:
    """Хвост прошлого возврата: пересылка до срока, отметка автоматического переезда — нет."""
    c.s.reserve_set_in("relay", relay_to="198.51.100.7:51820", relay_at=timeutil.now_iso(),
                       relay_until=timeutil.to_iso(timeutil.shift(timeutil.now(), 86400)))


def _majority(c) -> list[tuple]:
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "uns")
    return c.tick()


def _to_points(c) -> list[tuple]:
    """Канала пары нет — после «забираю» сразу по точкам."""
    c.b.online = False
    acts = _majority(c) + c.run(T_UP + 5)
    assert c.stage() == "points", f"подготовка: решение не дошло до точек: {c.stage()!r}"
    return acts


# ── relay — зона свободна (Q1-03/Q1-04) ─────────────────────────────────────

def test_in_relay_a_dead_primary_is_taken_by_points(c):
    _relay(c)
    acts = _to_points(c)
    assert sorted(s for s, _ in gw_sent(acts, "qtake")) == [1, 2], "в relay «забираю» не ушло"
    c.share(1)
    assert c.b.launched, "в relay точки отвергнуты — процесс открытия не запущен"
    assert c.s.reserve_in() == "taking", f"в relay открытие не пошло: {c.s.reserve_in()}"
    again = c.run(T_UP + ASK + NEED2)
    assert not gw_sent(again, "qtake") and not gw_sent(again, "qneed"), "после открытия кандидат повторяет решение"


def test_in_relay_the_code_window_outside_a_decision_opens(c):
    _relay(c)
    for slot in (1, 2):
        c.b.gone(slot)
    c.own("uns")
    acts = c.tick()
    assert c.s.reserve_code_window()["open"], "в relay окно кода вне решения не открылось"
    assert notes(acts), "в relay окно кода открыто без Т-К21"


def test_in_relay_the_quiet_code_window_of_a_tie_opens(c):
    _relay(c)
    c.s.reserve_code_window_quiet(True)
    assert c.s.reserve_code_window()["open"], "в relay окно кода ничьей не открылось"


def test_in_relay_the_interval_between_automatic_moves_still_holds(c):
    c.s.rsv_update("rsv_in", ended_auto=True)
    _relay(c)
    assert "между автоматическими переездами" in c.s.reserve_auto_block(), \
        "в relay пауза между автоматическими переездами не действует"


def test_in_relay_the_damper_after_a_failed_opening_still_holds(c):
    c.s.rsv_update("rsv_in", fail_at=timeutil.now_iso())
    _relay(c)
    assert "пауза две минуты" in c.s.reserve_auto_block(), "в relay демпфер после срыва не действует"


# ── стадия opened без открытия (TQ-1) ───────────────────────────────────────

def test_a_failure_before_the_next_tick_drops_the_opened_stage_and_retakes(c):
    _to_points(c)
    c.share(1)
    assert c.s.reserve_in() == "taking", "подготовка: открытие не пошло"
    c.s._take_fail("процесс открытия отказал")             # срыв раньше ближайшего такта кандидата
    first = c.run(5)
    assert c.stage() == "", f"после срыва стадия кандидата {c.stage()!r} — решение не снято"
    assert sorted(s for s, _ in gw_sent(first, "qrel")) == [1, 2], f"сорвавшееся «забираю» не откачено: {first}"
    acts = c.run(T_UP + DAMPER + ASK + 10)
    assert gw_sent(acts, "qtake"), "после срыва решение больше не исполняется"


def test_a_block_at_the_moment_of_the_points_drops_the_opened_stage(c):
    _to_points(c)
    settings.set_value("app.reserve.ready", False)          # готовность сняли, пока ждали точки
    c.share(1)
    assert not c.b.launched and c.s.reserve_in() == "idle", "подготовка: открытие пошло"
    settings.set_value("app.reserve.ready", True)
    c.run(5)
    assert c.stage() == "", f"открытие не пошло, а стадия {c.stage()!r}"
    acts = c.run(T_UP + 10)
    assert gw_sent(acts, "qtake"), "открытие не пошло — решение больше не исполняется"
    assert c.stage() == "points", f"повторное решение не дошло до точек: {c.stage()!r}"
    c.share(1)
    assert c.b.launched, "повторное решение не открыло зону по точкам"


def test_a_launch_error_drops_the_opened_stage(c):
    _to_points(c)
    c.b.launch_fail = "юнит не стартовал"
    c.share(1)
    assert c.s.reserve_in() == "idle", "подготовка: отказ запуска не вернул idle"
    c.run(5)
    assert c.stage() == "", f"отказ запуска, а стадия {c.stage()!r}"


# ── qrel после отданного qtake (TQ-2, сторона кандидата) ────────────────────

def test_own_see_after_qtake_sends_qrel_to_the_same_nodes(c):
    acts = _majority(c) + c.run(T_UP)
    took = gw_sent(acts, "qtake")
    assert sorted(s for s, _ in took) == [1, 2], f"подготовка: «забираю» не ушло: {took}"
    c.own("see")
    acts = c.run(5)
    rel = gw_sent(acts, "qrel")
    assert sorted(s for s, _ in rel) == [1, 2], f"свой «вижу» после qtake — qrel не тем или никому: {rel}"
    assert all(b == {"l": "b", "e": 7} for _, b in rel), f"qrel не под эпохой qtake: {rel}"
    assert not gw_sent(c.run(10), "qrel"), "qrel повторяется"


def test_a_lost_majority_after_qtake_sends_qrel(c):
    acts = _majority(c) + c.run(T_UP)
    assert gw_sent(acts, "qtake"), "подготовка: «забираю» не ушло"
    c.view(1, "see")
    c.view(2, "see")
    rel = gw_sent(c.run(5), "qrel")
    assert sorted(s for s, _ in rel) == [1, 2], f"большинство распалось после qtake — без qrel: {rel}"


def test_a_successful_move_keeps_the_raised_epoch(c):
    _to_points(c)
    c.share(1)
    c.run(5)
    c.s.reserve_set_in("serving", kind="dead")
    acts = c.run(10)
    c.s.reserve_set_in("idle")
    acts += c.run(10)
    assert not gw_sent(acts, "qrel"), f"переезд состоялся, а шлюзам ушёл откат эпохи: {gw_sent(acts, 'qrel')}"


# ── окно кода вне решения — только когда код что-то откроет (Q1-10) ─────────

@pytest.mark.parametrize("what", ["ready_off", "no_pkg", "no_box"])
def test_the_code_window_outside_a_decision_needs_the_open_preconditions(c, what):
    c.b.peer_admin_opened()
    if what == "ready_off":
        settings.set_value("app.reserve.ready", False)
    elif what == "no_pkg":
        c.s.rsv_put("rsv_pkg_in", {})
    else:
        q = c.s.rsv_get("rsv_qkeys")
        q.pop("peer", None)
        c.s.rsv_put("rsv_qkeys", q)
    assert c.s.reserve_open_block(), "подготовка: предусловия открытия выполнены"
    for slot in (1, 2):
        c.b.gone(slot)
    c.own("uns")
    acts = c.tick() + c.run(30)
    assert not c.s.reserve_code_window()["open"], f"{what}: окно кода открыто, а код ничего не откроет"
    assert not notes(acts), f"{what}: «введите код» при невыполнимых предусловиях: {[n.text[:60] for n in notes(acts)]}"


def test_the_code_window_outside_a_decision_opens_once_the_block_is_gone(c):
    settings.set_value("app.reserve.ready", False)
    for slot in (1, 2):
        c.b.gone(slot)
    c.own("uns")
    c.tick()
    assert not c.s.reserve_code_window()["open"], "подготовка: окно открыто при выключенной готовности"
    settings.set_value("app.reserve.ready", True)
    c.run(5)
    assert c.s.reserve_code_window()["open"], "готовность включили — окно кода не открылось"


# ── отметка автоматического переезда (Q5-04) ────────────────────────────────

def test_a_launch_error_of_an_opening_by_points_leaves_the_damper_not_the_interval(c):
    _to_points(c)
    c.b.launch_fail = "юнит не стартовал"
    c.share(1)
    inn = c.s.rsv_get("rsv_in")
    assert inn.get("state") == "idle" and inn.get("ended_auto") is False, f"отказ запуска оставил отметку: {inn}"
    assert "пауза две минуты" in c.s.reserve_auto_block(), f"после отказа запуска — не демпфер: {c.s.reserve_auto_block()!r}"
    c.b.advance(DAMPER + 5)
    assert c.s.reserve_auto_block() == "", f"после демпфера автопереезд всё ещё закрыт: {c.s.reserve_auto_block()!r}"


@pytest.mark.parametrize("kind,auto", [("button", False), ("dead", True), ("rf", True)])
def test_a_move_by_the_primary_key_sets_its_own_automatic_mark(c, monkeypatch, kind, auto):
    """Переезд с K основы: кнопкой — не автоматический (прежняя отметка не наследуется),
    по просьбе кворума (dead | rf) — автоматический."""
    from tests.integration._reserve_vote_common import PKG_GEN
    monkeypatch.setattr(c.s, "_rsvopen_launch", lambda payload, keep_stdin=False: None)
    c.s.rsv_update("rsv_in", ended_auto=not auto)              # прежний переезд — другого рода
    assert c.s.reserve_go_req_in(PKG_GEN, kind) == "", "подготовка: go_req отказан"
    assert c.s.rsv_get("rsv_in").get("ended_auto") is auto, f"{kind}: отметка {c.s.rsv_get('rsv_in').get('ended_auto')}"


def test_a_move_by_the_emergency_code_is_not_automatic(c):
    c.s.rsv_update("rsv_view", code={"open": True, "since": timeutil.now_iso(), "tries": [], "by": "quorum"})
    c.s.reserve_open_by_points({"code": "00" * 32, "cbox": "00"}, "code", "H4")
    inn = c.s.rsv_get("rsv_in")
    assert inn.get("state") == "taking", f"подготовка: открытие по коду не пошло: {inn}"
    assert inn.get("ended_auto") is False, "переезд по коду помечен автоматическим — интервал 30 мин закроет точки"


# ── причина переезда гостю (Q5-10) ──────────────────────────────────────────

@pytest.mark.parametrize("kind,case,word", [("dead", "q", "не отвечает"), ("rf", "q", "недоступен из РФ"),
                                            ("code", "H4", "аварийным кодом")])
def test_the_guest_gets_the_kind_of_the_move_as_its_reason(c, kind, case, word):
    from awgbot.bot.texts.reserve import guest as gt
    if kind == "code":
        c.s.rsv_update("rsv_view", code={"open": True, "since": timeutil.now_iso(), "tries": [], "by": "quorum"})
        secret = {"code": "00" * 32, "cbox": "00"}
    else:
        secret = {"qpoints": [[1, "ab"], [2, "cd"]], "qbox": "00"}
    c.s.reserve_open_by_points(secret, kind, case)
    inn = c.s.rsv_get("rsv_in")
    assert inn.get("state") == "taking" and inn.get("case") == case, f"подготовка: открытие не пошло: {inn}"
    v = c.s.reserve_guest_state_view()
    assert v["reason"] == kind, f"гостю причина {v['reason']!r} вместо вида переезда {kind!r}"
    assert word in gt.main_line(v), f"главная строка гостя без причины: {gt.main_line(v)!r}"


# ── просьба кандидата лидеру с большинством «да» (Q8-5) ─────────────────────

SINCE = "2026-10-30T10:05:00+03:00"


def _lead(svc, yes: int, *, fenced: bool = False):
    """Машина лидера своей линии a: голосующие b, a1, a2 (v = 3), «да» — первые yes свежими пробами."""
    import time as _t
    from awgbot.util.quorum.leader import Leader, Seen
    ld = Leader("a", 3, 7, "b", voters=("b", "a1", "a2"))
    ld.fenced, ld.starting = fenced, False
    now = _t.monotonic()
    for n in ("b", "a1", "a2")[:yes]:
        ld.seen[n] = Seen("see", 7, 0, now)
    svc.__dict__["_rsv_qleader"] = ld
    return ld


@pytest.fixture()
def lead_world(services, rsv, monkeypatch):
    rc.pair_row(services)
    moves: list[dict] = []
    monkeypatch.setattr(services, "reserve_move_check", lambda: "")
    monkeypatch.setattr(services, "reserve_move_start", lambda **kw: moves.append(kw) or {})
    return services, moves


def test_a_leader_with_a_yes_majority_refuses_the_quorum_ask(lead_world):
    svc, moves = lead_world
    ld = _lead(svc, yes=2)
    assert ld.yes_count(__import__("time").monotonic()) == 2, "подготовка: «да» не два"
    acts = svc.reserve_ask_in("q", SINCE)
    assert acts == [("send", "ask_no", {"reason": "quorum"})], f"лидер с большинством «да» согласился: {acts}"
    assert moves == [], "лидер с большинством «да» отдал пользователей по одному слову канала пары"


@pytest.mark.parametrize("yes,fenced", [(1, False), (0, False), (3, True)], ids=["minority", "none", "fenced"])
def test_a_leader_without_a_yes_majority_or_fenced_hands_over(lead_world, yes, fenced):
    svc, moves = lead_world
    _lead(svc, yes=yes, fenced=fenced)
    acts = svc.reserve_ask_in("q", SINCE)
    assert acts == [("live",)] and moves == [{"kind": "dead", "dead_since": SINCE}], f"лидер без кворума не отдал: {acts}"


def test_the_candidate_refused_with_quorum_goes_by_points_after_ask_seconds(c):
    """Отказ «у лидера кворум» — не «занят»: просьба не повторяется, через ask_seconds — точки
    (их дадут только шлюзы, которые сами лидера не видят); цикла просьб нет."""
    from tests.integration._reserve_vote_common import sent
    acts = _majority(c) + c.run(T_UP)
    assert len(sent(acts, "ask")) == 1, "подготовка: просьба лидеру не ушла"
    assert c.s.reserve_ask_no_in("quorum") == []
    acts = c.run(ASK + 5)
    assert not sent(acts, "ask"), "после отказа «у лидера кворум» просьба повторена"
    assert sorted({s for s, _ in gw_sent(acts, "qneed")}) == [1, 2] and c.stage() == "points"


# ── цифры кворума лидера и кандидата (Q8-6) ─────────────────────────────────

def _peer_digits(c, **over) -> None:
    tm = {**c.s.reserve_quorum_timing_digits(), **over}
    quo = dict(c.s.rsv_get("rsv_peer_state").get("quo") or {})
    c.s.rsv_update("rsv_peer_state", quo={**quo, "tm": tm})


def test_the_st_quo_carries_the_timing_digits(c):
    from awgbot.util.quorum.timing import Timing
    t = Timing()
    assert c.s.reserve_quorum_timing_digits() == {"p": t.probe_s, "no": t.no_s, "w": t.yes_window_s, "down": t.down_s,
                                                  "up": t.up_s, "rf": t.rf_block_s}


def test_equal_digits_keep_the_candidate(c):
    _peer_digits(c)
    acts = _majority(c) + c.run(T_UP)
    assert gw_sent(acts, "qtake"), "цифры сходятся, а кандидата нет"


def test_diverging_digits_switch_the_candidate_off_and_leave_the_link_signal(c):
    c.b.peer_admin_opened()
    _peer_digits(c, down=60)                                # лидер ограждается позже, чем рассчитывает кандидат
    assert c.s.reserve_quorum_peer_timing_off() == "down"
    acts = _majority(c) + c.run(T_UP)
    assert not gw_sent(acts, "qtake") and not gw_sent(acts, "qask"), "цифры расходятся, а кандидат решает по своим"
    assert c.s.__dict__.get("_rsv_qcand") is None
    c.b.primary(False, since=timeutil.now_iso())
    acts = c.run(T_UP + 10) + c.s.reserve_code_link_watch()
    assert c.s.reserve_code_window()["open"], "цифры расходятся — запасного окна кода по линку пары нет"
    texts = [n.text for n in notes(acts)]
    assert any("цифры времени не сходятся" in t for t in texts), f"в Т-К21д нет причины: {texts}"
