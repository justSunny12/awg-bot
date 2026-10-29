"""Правило кандидата: счёт «нет», опрос, решение, случай 3, ничья
(util/quorum/candidate.py; концепт «кворум» §3.5, §3.7.5, §3.7.7).

Цена ошибки: «забираю» без большинства «нет» или раньше T_up — второй
писатель при живом лидере; «забираю», не снятое «вижу» или молчанием, — то же
после того, как связь вернулась; опрос, не отпущенный `qrel`, держит
обязательства шлюзов до 10 минут; ничья без тревоги — линия молча стоит без
переезда; тревога ничьей при каждом такте — шум со звуком у трёх админов.
"""
from __future__ import annotations

import itertools

import pytest

from awgbot.util.quorum.candidate import Candidate
from awgbot.util.quorum.members import majority
from awgbot.util.quorum.timing import Timing

pytestmark = pytest.mark.unit

T = Timing()
T0 = 1000.0


def _cand(v: int) -> Candidate:
    """Кандидат «b» линии «a»: голосующие — он сам и шлюзы a1…a(v−1)."""
    c = Candidate(l="a", v=v, me="b", timing=T)
    c.set_members(("b",) + tuple(f"a{i}" for i in range(1, v)), v)
    return c


def _run(c: Candidate, start: float, end: float, step: float = 1.0) -> list[tuple[float, str]]:
    out, now = [], start
    while now <= end:
        out += [(now, a) for a in c.tick(now)]
        now += step
    return out


def _first(events, kind: str) -> float | None:
    return next((t for t, a in events if a == kind or a.startswith(kind + ":")), None)


# ── v = 2 ─────────────────────────────────────────────────────────────────────

def test_v2_two_noes_take_after_t_up_without_asking():
    """Один шлюз: свой «не вижу» и «не вижу» шлюза — 2 > 1, переезд через T_up;
    опрашивать некого (§3.7.7)."""
    c = _cand(2)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    ev = _run(c, T0, T0 + 200)
    assert _first(ev, "ask") is None, "опрос без адресатов"
    assert [e for e in ev if e[1] == "take"] == [(T0 + T.up_s, "take")]


def test_v2_own_unseen_and_a_silent_gateway_never_take():
    c = _cand(2)
    c.set_own("uns", T0)
    ev = _run(c, T0, T0 + 1000, 5)
    assert ev == [], f"без «нет» шлюза кандидат что-то делает: {ev[:3]}"


def test_v2_own_see_and_gateway_unseen_is_a_tie_not_a_take():
    """v = 2, кандидат видит, шлюз нет — ничья: переезда нет, тревога через T_tie."""
    c = _cand(2)
    c.set_own("see", T0)
    c.view_in("a1", "uns", T0)
    ev = _run(c, T0, T0 + 1000, 5)
    assert "take" not in [a for _, a in ev]
    assert [e for e in ev if e[1] == "tie"] == [(T0 + T.tie_s, "tie")]


# ── v = 3 ─────────────────────────────────────────────────────────────────────

def test_v3_own_and_one_gateway_ask_the_other_and_repeat():
    """v = 3: свой uns и uns Шлюза 1 — уже 2 > 1,5; Шлюзу 2 уходит опрос сразу и
    повтор через 30 с; «забираю» — через T_up с момента большинства, третий
    голос момент не сдвигает."""
    c = _cand(3)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    ev = _run(c, T0, T0 + 40)
    asks = [(t, a) for t, a in ev if a.startswith("ask:")]
    assert asks == [(T0, "ask:a2"), (T0 + T.ask_repeat_s, "ask:a2")]
    c.view_in("a2", "uns", T0 + 50)
    ev = _run(c, T0 + 41, T0 + 200)
    assert _first(ev, "ask") is None, "опрос тому, кто уже сказал «нет»"
    assert [e for e in ev if e[1] == "take"] == [(T0 + T.up_s, "take")]


def test_v3_own_unseen_without_any_gateway_no_neither_asks_nor_takes():
    """Свой «не вижу» без единого «нет» шлюзов — чаще всего сеть самого кандидата."""
    c = _cand(3)
    c.set_own("uns", T0)
    c.view_in("a1", "see", T0)
    c.view_in("a2", "see", T0)
    assert _run(c, T0, T0 + 1000, 5) == []


def test_a_see_that_drops_the_majority_releases_and_restarts():
    c = _cand(3)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    assert c.tick(T0) == ["ask:a2"]
    c.view_in("a1", "see", T0 + 60)
    out = c.tick(T0 + 60)
    assert "rel" in out, "опрашивали, а «нет» упало — без qrel обязательства висят"
    assert c.take_deadline() is None
    c.view_in("a1", "uns", T0 + 70)
    ev = _run(c, T0 + 70, T0 + 250)
    assert [e for e in ev if e[1] == "take"] == [(T0 + 70 + T.up_s, "take")], "таймер не начался сначала"


def test_a_dead_channel_drops_the_no_like_silence():
    """«Нет» считается, пока канал голосующего жив: лёг канал — как молчание."""
    c = _cand(3)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    c.tick(T0)
    c.channel("a1", False)
    assert "rel" in c.tick(T0 + 10)
    assert "take" not in [a for _, a in _run(c, T0 + 11, T0 + 300, 5)]
    c.channel("a1", True)
    ev = _run(c, T0 + 301, T0 + 500)
    assert _first(ev, "take") == T0 + 301 + T.up_s


def test_a_drop_without_a_prior_ask_sends_no_release():
    c = _cand(2)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    c.tick(T0)
    c.view_in("a1", "see", T0 + 10)
    assert c.tick(T0 + 10) == [], "qrel без опроса"


def test_take_is_given_once():
    c = _cand(2)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    ev = _run(c, T0, T0 + 1000, 5)
    assert [a for _, a in ev].count("take") == 1


def test_blocked_releases_and_does_not_take_at_once():
    """Предусловия открытия не прошли — `rel` всем, «забираю» снято; новый отсчёт
    T_up — от следующего такта."""
    c = _cand(3)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    ev = _run(c, T0, T0 + T.up_s)
    assert _first(ev, "take") == T0 + T.up_s
    assert c.blocked() == ["rel"]
    assert c.take_deadline() is None
    ev = _run(c, T0 + T.up_s + 1, T0 + 2 * T.up_s)
    assert "take" not in [a for _, a in ev], "после отказа предусловий «забираю» сразу снова"


# ── v = 4 ─────────────────────────────────────────────────────────────────────

def test_v4_two_noes_ask_and_the_third_starts_t_up():
    """v = 4: свой uns и uns Шлюза 1 — 2, не > 2: опрос двоим; третий uns —
    большинство, «забираю» через T_up с этого момента."""
    c = _cand(4)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    c.view_in("a2", "see", T0)
    c.view_in("a3", "see", T0)
    assert c.tick(T0) == ["ask:a2,a3"]
    assert c.take_deadline() is None
    c.view_in("a2", "uns", T0 + 15)
    ev = _run(c, T0 + 15, T0 + 200)
    assert _first(ev, "take") == T0 + 15 + T.up_s


# ── случай 3 ─────────────────────────────────────────────────────────────────

def test_case_three_takes_after_rf_block():
    """Кандидат сам видит лидера, шлюзы — нет (> v/2): ждём T_up_rf = 900 с, без опроса."""
    c = _cand(3)
    c.set_own("see", T0)
    c.view_in("a1", "uns", T0)
    c.view_in("a2", "uns", T0)
    ev = _run(c, T0, T0 + 1000, 5)
    assert _first(ev, "ask") is None, "свой взгляд «вижу» — опроса нет"
    assert [e for e in ev if e[1] == "take"] == [(T0 + T.rf_block_s, "take")]


@pytest.mark.parametrize("tb_off,want_off", [(300, 400), (850, 900), (10, 110)])
def test_case_three_with_own_unseen_later_takes_at_the_earlier_deadline(tb_off, want_off):
    """Свой взгляд стал uns в tb — «забираю» не раньше min(начало + 900, tb + 100)."""
    c = _cand(3)
    c.set_own("see", T0)
    c.view_in("a1", "uns", T0)
    c.view_in("a2", "uns", T0)
    _run(c, T0, T0 + tb_off - 1)
    c.set_own("uns", T0 + tb_off)
    ev = _run(c, T0 + tb_off, T0 + 1100)
    assert [e for e in ev if e[1] == "take"] == [(T0 + want_off, "take")]


def test_case_three_ends_when_the_gateways_see_again():
    c = _cand(3)
    c.set_own("see", T0)
    c.view_in("a1", "uns", T0)
    c.view_in("a2", "uns", T0)
    _run(c, T0, T0 + 500, 5)
    c.view_in("a2", "see", T0 + 505)
    ev = _run(c, T0 + 505, T0 + 1500, 5)
    assert "take" not in [a for _, a in ev]
    assert c.take_deadline() is None


# ── ничья ────────────────────────────────────────────────────────────────────

def _tie4() -> Candidate:
    c = _cand(4)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    c.view_in("a2", "see", T0)
    c.view_in("a3", "see", T0)
    return c


def test_v4_two_against_two_is_a_tie_told_once():
    """«Да» = «нет» = 2 без молчащих дольше T_tie — одно сообщение на эпизод."""
    c = _tie4()
    ev = _run(c, T0, T0 + 1000, 5)
    assert [e for e in ev if e[1] == "tie"] == [(T0 + T.tie_s, "tie")]
    assert "take" not in [a for _, a in ev] and "tie_over" not in [a for _, a in ev]


def test_a_silent_voter_breaks_the_tie():
    c = _tie4()
    c.channel("a3", False)
    ev = _run(c, T0, T0 + 1000, 5)
    assert "tie" not in [a for _, a in ev], "ничья с молчащим голосующим"


def test_a_voter_that_never_spoke_is_silent_and_there_is_no_tie():
    c = _cand(4)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    c.view_in("a2", "see", T0)
    assert "tie" not in [a for _, a in _run(c, T0, T0 + 1000, 5)]


def test_tie_over_after_a_minute_without_the_tie():
    c = _tie4()
    _run(c, T0, T0 + T.tie_s, 5)
    c.view_in("a3", "uns", T0 + 200)              # 3 из 4 — уже не ничья
    ev = _run(c, T0 + 200, T0 + 400, 5)
    assert [e for e in ev if e[1] == "tie_over"] == [(T0 + 200 + T.tie_clear_s, "tie_over")]
    assert [a for _, a in ev].count("tie") == 0


def test_a_tie_that_comes_back_within_the_minute_is_the_same_episode():
    c = _tie4()
    _run(c, T0, T0 + T.tie_s, 5)
    c.view_in("a3", "uns", T0 + 200)
    _run(c, T0 + 200, T0 + 230, 5)
    c.view_in("a3", "see", T0 + 235)
    ev = _run(c, T0 + 235, T0 + 1000, 5)
    acts = [a for _, a in ev]
    assert "tie" not in acts and "tie_over" not in acts, f"дребезг ничьей дал сообщения: {acts}"


def test_no_tie_over_when_no_tie_was_told():
    c = _tie4()
    _run(c, T0, T0 + 100, 5)
    c.view_in("a3", "uns", T0 + 105)
    assert "tie_over" not in [a for _, a in _run(c, T0 + 105, T0 + 400, 5)]


# ── таблица истинности кандидата ─────────────────────────────────────────────

VIEWS = ("see", "sus", "uns", "held", None)


@pytest.mark.parametrize("v", [2, 3, 4, 5])
def test_truth_table_of_the_candidate(v):
    """Все сочетания (свой взгляд + взгляды шлюзов): «забираю» через T_up только при
    своём uns и «нет» > v/2; при своём see/held и «нет» шлюзов > v/2 — только через
    T_up_rf; иначе — никогда."""
    for combo in itertools.product(VIEWS, repeat=v):
        c = _cand(v)
        own, rest = combo[0], combo[1:]
        if own is not None:
            c.set_own(own, T0)
        for i, w in enumerate(rest, 1):
            if w is not None:
                c.view_in(f"a{i}", w, T0)
        no = sum(1 for w in combo if w == "uns")
        take = _first(_run(c, T0, T0 + T.rf_block_s + 10, 5), "take")
        if own == "uns" and majority(no, v):
            want = T0 + T.up_s
        elif own != "uns" and majority(no, v):
            want = T0 + T.rf_block_s
        else:
            want = None
        assert take == want, f"v={v} {combo}: «забираю» в {take}, ожидалось {want}"
