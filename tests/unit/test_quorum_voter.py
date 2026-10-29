"""Правило голосующего: взгляд по ответам на пробы, досылки, обязательство «нет»,
известная эпоха (util/quorum/voter.py; концепт «кворум» §2.4, §2.8, §3.2, §3.4.3).

Цена ошибки: взгляд, не переходящий в `sus` при пропавшем ответе, — пробы со
`see` держат лидера, хотя голосующий его уже не видит, и тот же голос уходит
кандидату «нет» (голос дважды); «вижу» по одному случайному ответу — дребезг
`qview` и таймеров кандидата; обязательство, которое снимает чужое слово или
не снимает своё, — лидер возвращается к записи при живом новом держателе или
шлюз навсегда не даёт ему «да».
"""
from __future__ import annotations

import pytest

from awgbot.util.quorum.probe import Reply
from awgbot.util.quorum.timing import Timing
from awgbot.util.quorum.voter import Voter

pytestmark = pytest.mark.unit

T = Timing()
T0 = 1000.0


def _reply(s: int, e: int = 12) -> Reply:
    return Reply("a", 4, e, "lead", s, 2, 3)


def _voter() -> Voter:
    v = Voter(l="a", me="a1", timing=T)
    v.start(T0)
    v.epoch_in(12)
    return v


def _probe_answered(v: Voter, now: float, e: int = 12) -> list[str]:
    s = v.probe_out(now, T.probe_s)
    return v.reply_in(_reply(s, e), now + 0.1)


def _seeing() -> Voter:
    v = _voter()
    _probe_answered(v, T0)
    _probe_answered(v, T0 + 15)
    assert v.view == "see"
    return v


# ── взгляд ───────────────────────────────────────────────────────────────────

def test_see_from_new_needs_two_replies():
    v = _voter()
    assert v.tick(T0) == ["probe"]
    assert _probe_answered(v, T0) == [], "«вижу» по одному ответу из new"
    assert v.view == "new"
    assert _probe_answered(v, T0 + 15) == ["see"]
    assert v.w_for_probe() == "see"


def test_a_lost_reply_makes_sus_and_schedules_resends():
    v = _seeing()
    v.probe_out(T0 + 30, T.probe_s)                  # ответа не будет
    assert v.tick(T0 + 32) == []
    assert v.tick(T0 + 33) == ["sus"]
    assert v.w_for_probe() == "sus"
    assert v.next_probe == T0 + 33 + T.resend_s, "досылка не через 5 с"
    assert v.tick(T0 + 38) == ["probe"]


def test_sus_back_to_see_with_one_reply():
    v = _seeing()
    v.probe_out(T0 + 30, T.probe_s)
    v.tick(T0 + 33)
    assert _probe_answered(v, T0 + 38) == ["see"]


def test_no_reply_for_no_seconds_is_uns_once():
    """«Не вижу» — 45 с без ответа с момента последнего ответа; уходит один раз."""
    v = _seeing()
    last = T0 + 15.1
    out = []
    now = T0 + 16
    while now <= T0 + 200:
        out += [(now, a) for a in v.tick(now) if a in ("uns", "sus")]
        if v.next_probe <= now:
            v.probe_out(now, T.probe_s)
        now += 1
    assert [a for _, a in out] == ["sus", "uns"]
    assert out[1][0] == pytest.approx(last + T.no_s, abs=1), "uns не через no_s от последнего ответа"


def test_a_voter_that_never_got_a_reply_turns_uns_after_no_seconds_from_start():
    v = _voter()
    acts = []
    for k in range(0, 60):
        acts += [(T0 + k, a) for a in v.tick(T0 + k) if a == "uns"]
    assert acts == [(T0 + T.no_s, "uns")]


def test_see_from_uns_needs_two_replies_in_a_row():
    """«Вижу» из uns — два ответа подряд: ответ, потерянный ответ, ответ — ещё не
    «вижу» (гистерезис против дребезга)."""
    v = _voter()
    for k in range(0, 50):
        v.tick(T0 + k)
    assert v.view == "uns"
    _probe_answered(v, T0 + 50)
    v.probe_out(T0 + 60, T.probe_s)
    v.tick(T0 + 64)                                  # ответ пропал
    assert _probe_answered(v, T0 + 70) == [], "ответы с пропуском между ними засчитаны как два подряд"
    assert v.view == "uns"


def test_a_reply_to_an_unknown_probe_or_another_line_is_ignored():
    v = _seeing()
    v.probe_out(T0 + 30, T.probe_s)
    assert v.reply_in(_reply(99999), T0 + 30.5) == []
    assert v.reply_in(Reply("b", 4, 12, "lead", v.seq, 1, 2), T0 + 30.5) == []
    assert v.tick(T0 + 33) == ["sus"], "чужой ответ принят за свой"


def test_a_reply_with_a_greater_epoch_is_adopted_once():
    v = _seeing()
    assert _probe_answered(v, T0 + 30, e=13) == ["adopt:13"]
    assert v.epoch == 13
    assert _probe_answered(v, T0 + 45, e=13) == []
    assert _probe_answered(v, T0 + 60, e=11) == [] and v.epoch == 13, "эпоха пошла назад"


def test_epoch_from_state_only_grows():
    v = _voter()
    assert v.epoch_in(14) and v.epoch == 14
    assert not v.epoch_in(13) and v.epoch == 14


def test_a_pause_holds_uns_back_for_at_most_five_minutes():
    """`qpause` лидера — «не вижу» не уходит до срока, но не дольше pause_s."""
    v = _seeing()
    v.pause(T0 + 20, 3600)
    acts = [(k, a) for k in range(20, 400) for a in v.tick(T0 + k) if a == "uns"]
    assert acts == [(20 + T.pause_s, "uns")]


# ── обязательство «нет» ──────────────────────────────────────────────────────

def test_after_said_no_a_restored_see_goes_out_as_held():
    v = _voter()
    for k in range(0, 50):
        v.tick(T0 + k)
    v.said_no(T0 + 50)
    _probe_answered(v, T0 + 60)
    _probe_answered(v, T0 + 75)
    assert v.view == "see"
    assert v.w_for_probe() == "held", "под обязательством проба несёт «вижу»"


def test_release_of_another_epoch_does_not_lift_the_pledge():
    v = _seeing()
    v.said_no(T0 + 20)
    assert not v.release(13)
    assert v.w_for_probe() == "held"
    assert v.release(12)
    assert v.w_for_probe() == "see"
    assert not v.release(12), "повторный qrel что-то снял"


def test_take_moves_the_epoch_forward_and_lifts_the_pledge():
    v = _seeing()
    v.said_no(T0 + 20)
    assert v.take(12)
    assert v.epoch == 13 and v.w_for_probe() == "see"
    v.take(5)
    assert v.epoch == 13, "qtake со старой эпохой отмотал эпоху назад"


def test_the_pledge_expires_after_pledge_seconds_once():
    v = _seeing()
    v.said_no(T0 + 20)
    acts = [(k, a) for k in range(21, 1300, 1) for a in v.tick(T0 + k) if a == "unpledge"]
    assert acts == [(20 + T.pledge_s, "unpledge")]


def test_uns_under_a_pledge_stays_uns():
    v = _voter()
    for k in range(0, 50):
        v.tick(T0 + k)
    v.said_no(T0 + 50)
    assert v.w_for_probe() == "uns"
