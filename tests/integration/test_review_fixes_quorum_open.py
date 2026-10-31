"""Правки по ревью исполнения решения кандидата кворума на стороне резерва
(domain/services/reserve/quorum/candidate.py, codewin.py, opening.py):

- фаза relay (пересылка хвоста прошлого возврата) — зона свободна и для исполнения:
  точки принимаются, процесс открытия идёт, окно кода открывается, паузы
  автоматического переезда (`min_interval_minutes`, демпфер) — те же, что в idle;
- открытие по точкам не пошло (предусловия к моменту точек, отказ запуска, срыв
  раньше такта) — стадия `opened` снимается, решение исполняется заново обычным путём;
- отданный `qtake` помнится до исхода: снятие исполнения без переезда (свой «вижу»
  вернулся, распад большинства, срыв открытия) — `qrel{e}` тем же узлам.

Мир — кандидат `Cand` поверх `box` (tests/integration/_reserve_vote_common.py): резерв
«Alpha» (сторона a), линия «Beta» b с составом b, a, b1, b2 (v = 3, t = 2). Хост и сеть
не трогаются.

Цена ошибки: основа умерла, пока резерв держит хвост прошлого возврата (до 2 ч тишины
или 7 дней трафика), — каждые T_up «забираю» и просьба точек впустую, точки отвергаются,
окна кода нет: пользователи без VPN, админ не знает; стадия `opened` после срыва — кандидат
немеет до распада большинства; шлюзы остаются с поднятой «забираю» эпохой e + 1 — клон
под e + 1 из пакета не станет писателем, прежний лидер под e потеряет «да».
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
