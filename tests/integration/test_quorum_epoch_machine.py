"""Машина лидера кворума и эпоха журнала — одно целое (domain/services/reserve/quorum/
leader.py, fence.py; outbound.py `_tick_away`; outbound_stuck.py): право записи даёт и
отнимает журнал, машина лишь считает «да» и подстраивается под журнал, а не
наоборот. Сорванное «забираю» кандидата (его `qtake` поднял эпоху шлюзов, а
открытие не пошло) и срыв переезда у лишённой кворума основы не оставляют её без
права записи навсегда.

- принятие чужой эпохи по пробе огораживает машину: «да» большинства одни запись
  не возвращают, пока вторая сторона не сказала после принятия, что моих не несёт;
- в away по принятой эпохе (`by = share`) живая вторая сторона со свежим `st` без
  моих пользователей возвращает домой; поллинг мой; факт запоминается
  (`idle_seen`), и устаревший потом `st` дребезга не даёт; большинство «да» за
  окно U возвращает эпоху сюда (+1);
- зависший вебхук (`take_home`) при принятой эпохе снимает отметку принятия —
  роль дома возвращает эпоху, машина следует за журналом;
- срыв переезда на шаге record у лишённой кворума основы пересоздаёт машину
  ограждённой (журнал self/follower): большинство «да» за окно U снимает ограждение;
- в ходе передачи кнопкой (leaving до released) проба с эпохой e + 1 не принимается:
  это поднятая кандидатом эпоха, а не факт — иначе страховка такта давала бы e + 2 и
  ложный хвост С9;
- принявшая эпоху основа шлёт второй стороне `qstate` — та сбрасывает отпечаток
  `st` и присылает свежий (факт для возврата); эпоха вне разумного не принимается.

Мир — основа «Alpha» дома в паре с «Beta» (tests/integration/_reserve_move.py),
линия `a` с составом a, b, a1, a2 (`_Q` из test_reserve_quorum_cov_leader.py: часы
лидера модельные, пробы — настоящие датаграммы).

Цена ошибки: основа после сорванного «забираю» — follower бессрочно (каждая правка
админа получает отказ, бот молчит пользователям, которые никуда не уезжали), шлюзы
держат пересылку на резерв; при 3 из 3 «да» карточка говорит «без кворума»,
пользователям — Т-К9.
"""
from __future__ import annotations

import time

import pytest

from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_quorum_cov_leader import _Q, _fence, _lead, U, W

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

YES = ("b", "a1", "a2")


@pytest.fixture()
def q(mv, monkeypatch):
    return _Q(mv.svc, monkeypatch)


def _peer_down(q, monkeypatch) -> None:
    monkeypatch.setattr(q.s, "reserve_peer_seen", lambda: "down")


def _peer_idle_now(q, monkeypatch) -> None:
    """Вторая сторона жива, её свежий `st` (после всего, что было до вызова): моих не несёт."""
    monkeypatch.setattr(q.s, "reserve_peer_seen", lambda: "ok")
    q.s.rsv_update("rsv_peer_state", session=True, inn="idle", st_stale=False,
                   at=timeutil.to_iso(timeutil.shift(timeutil.now(), 5)), at_ts=time.time() + 5)


def _share_away(q, monkeypatch) -> int:
    """Лишённая кворума основа принимает e + 1 по пробе шлюза, пара молчит → away по принятой эпохе."""
    _fence(q)
    e = q.s.reserve_quorum_epoch()
    _peer_down(q, monkeypatch)
    q.probe("a1", e=e + 1)
    assert q.meta() == ("peer", "follower", e + 1), "подготовка: эпоха по пробе не принята"
    assert q.s._qleader.fenced, "принятие эпохи не огородило машину лидера"
    q.s.reserve_out_tick()
    out = q.s.rsv_get("rsv_out")
    assert out.get("state") == "away" and out.get("by") == "share", f"подготовка: {out}"
    return e


def _run_yes(q, seconds: int, e: int) -> list[tuple]:
    acts: list[tuple] = []
    for i in range(seconds // 5):
        q.t += 5
        if i % 3 == 0:
            for n in YES:
                q.probe(n, e=e)
        acts += q.tick()
        q.s.reserve_out_tick()
    return acts


# ── сорванное «забираю» ─────────────────────────────────────────────────────

def test_a_cancelled_take_comes_home_on_the_peers_word_and_the_majority_returns_the_epoch(q, monkeypatch):
    e = _share_away(q, monkeypatch)
    _peer_idle_now(q, monkeypatch)
    acts = q.s.reserve_out_tick()
    assert q.s.reserve_out() == "home", "живая вторая сторона без моих — а основа в away"
    assert q.s.reserve_poll() == "mine" and ("wake_poll",) in acts
    assert q.meta()[0] == "peer", "эпоха вернулась без правила лидера"
    _run_yes(q, U + 20, e + 1)
    assert q.meta() == ("self", "holder", e + 2), f"большинство «да» при факте второй стороны не вернуло запись: {q.meta()}"
    assert not q.s.reserve_quorum_adopted(), "отметка принятия не снята"
    assert not q.s._qleader.fenced and not q.s.reserve_quorum_fenced()


def test_a_cancelled_take_with_a_dead_peer_is_undone_by_the_stuck_webhook_path(q, monkeypatch):
    e = _share_away(q, monkeypatch)
    q.s.reserve_stuck_home()                                  # зависший вебхук: take_home прошёл, запись и пользователи здесь
    assert not q.s.reserve_quorum_adopted(), "take_home оставил отметку принятия — роль дома следовала бы бессрочно"
    q.s.reserve_epoch_check()
    assert q.meta() == ("self", "holder", e + 2), f"дома без отметки принятия эпоха не вернулась: {q.meta()}"
    q.tick()
    assert not q.s._qleader.fenced, "машина лидера ограждена при журнале-держателе"
    _run_yes(q, W, e + 2)                                     # голосующие узнали новую эпоху из ответов — «да» под ней
    assert q.meta() == ("self", "holder", e + 2) and not q.s._qleader.fenced, "лидер с большинством «да» снова лишён кворума"


def test_the_peers_word_from_before_the_adoption_keeps_me_away(q, monkeypatch):
    _share_away(q, monkeypatch)
    monkeypatch.setattr(q.s, "reserve_peer_seen", lambda: "ok")
    q.s.rsv_update("rsv_peer_state", session=True, inn="idle", st_stale=False,
                   at=timeutil.to_iso(timeutil.shift(timeutil.now(), -60)), at_ts=time.time() - 60)
    q.s.reserve_out_tick()
    assert q.s.reserve_out() == "away", "старый st второй стороны (до принятия) вернул домой"


def test_adopting_while_leading_fences_the_machine_and_the_fact_unfences_it(q, monkeypatch):
    """Машина без ограждения (лидер) принимает e + 1 по пробе: без ограждения правило никогда
    не выдало бы `unfence`, и запись не вернулась бы даже при живой idle второй стороне."""
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    _peer_idle_now(q, monkeypatch)
    q.probe("a1", e=e + 1)
    assert q.meta() == ("peer", "follower", e + 1)
    assert q.s._qleader.fenced and q.s._qleader.role() == "fenced", "машина лидера после принятия эпохи не ограждена"
    _peer_idle_now(q, monkeypatch)                            # свежий st после принятия
    _run_yes(q, U + 20, e + 1)
    assert q.meta() == ("self", "holder", e + 2), f"запись не вернулась: {q.meta()}"


# ── срыв переезда у лишённой кворума основы ─────────────────────────────────

def test_a_move_failed_at_record_by_a_fenced_leader_keeps_the_machine_fenced_until_the_majority(q):
    _fence(q)
    e = q.s.reserve_quorum_epoch()
    rm.to_step(q.s, "record", kind="rf")
    q.tick()
    assert q.s._qleader.q2, "подготовка: на шаге record машина не в отъезде"
    q.s._move_fail("запись не изменена за 30 минут", released=True)
    assert q.s.reserve_out() == "home" and q.meta()[:2] == ("self", "follower")
    q.tick()
    assert q.s._qleader.fenced and not q.s._qleader.starting, "машина пересоздана без ограждения при журнале follower"
    q.t += 5
    q.tick()
    assert q.meta()[:2] == ("self", "follower"), "ограждение снято без окна U"
    _run_yes(q, U + 20, e)
    assert q.meta()[:2] == ("self", "holder"), f"большинство «да» не вернуло запись: {q.meta()}"
    assert not q.s.reserve_quorum_fenced()


# ── передача кнопкой ────────────────────────────────────────────────────────

def test_a_probe_with_a_raised_epoch_during_the_hand_over_is_not_adopted(q):
    _fence(q)
    e = q.s.reserve_quorum_epoch()
    acts = q.s.reserve_ask_in("q", timeutil.now_iso(), "rf")
    assert q.s.reserve_out() == "leaving", f"подготовка: просьба кворума не повела в leaving: {acts}"
    q.probe("b", e=e + 1)                                     # кандидат поднял свою эпоху после «забираю» — не факт
    assert q.meta() == ("self", "follower", e), "эпоха пробы принята в ходе передачи"
    assert not q.s.reserve_quorum_adopted()
    q.s.reserve_out_tick()
    assert q.meta()[2] == e and not q.s.rsv_get("rsv_c9"), "страховка такта дала e + 2 и ложный хвост С9"


# ── qstate второй стороне, эпоха вне разумного ──────────────────────────────

def test_adoption_asks_the_peer_for_a_fresh_st_with_qstate(q, monkeypatch):
    _fence(q)
    e = q.s.reserve_quorum_epoch()
    _peer_down(q, monkeypatch)
    q.s.rsv_update("rsv_peer_state", session=True)
    q.probe("a1", e=e + 1)
    acts = q.tick()
    sent = [a for a in acts if a[0] == "send" and a[1] == "qstate"]
    assert sent and sent[0][2]["e"] == e + 1 and sent[0][2]["holder"] == "peer", f"qstate второй стороне не ушёл: {acts}"
    assert not [a for a in q.tick() if a[0] == "send" and a[1] == "qstate"], "qstate уходит на каждый такт"


def test_an_incoming_qstate_makes_my_next_st_go_out_again(q):
    assert q.s.reserve_st_if_changed() is not None
    assert q.s.reserve_st_if_changed() is None, "подготовка: st без изменений ушёл бы снова"
    q.s.reserve_quorum_pair_in("qstate", {"l": "b", "e": 3, "holder": "peer", "r": "follower"})
    assert q.s.reserve_st_if_changed() is not None, "после qstate второй стороны свежий st не уходит"


def test_an_epoch_far_beyond_mine_is_not_adopted(q):
    _lead(q)
    e = q.s.reserve_quorum_epoch()
    q.probe("a1", e=e + 1001)
    assert q.meta() == ("self", "holder", e) and not q.s.reserve_quorum_adopted()
    q.s.reserve_quorum_pair_in("qstate", {"l": "a", "e": e + 1001, "holder": "self", "r": "holder"})
    assert q.meta() == ("self", "holder", e)
