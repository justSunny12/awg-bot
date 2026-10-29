"""Правило лидера: счёт «да», лишение кворума, случай 3, старт и возврат
(util/quorum/leader.py; концепт «кворум» §3.4, §3.7.7, §3.7.12, §2.2).

Цена ошибки: лидер, который держится при «да» < v/2, пишет в базу, когда
кандидат уже мог забрать пользователей, — два писателя одной линии; лидер,
который лишается кворума при «да» ≥ v/2, — бот основы глохнет на ровном месте;
«да» под чужой эпохой, прежним поколением после окна или со взглядом `held` в
счёте — возврат к записи при живом новом держателе; старт с правом записи —
то же после перезапуска.
"""
from __future__ import annotations

import itertools

import pytest

from awgbot.util.quorum.leader import Leader
from awgbot.util.quorum.members import half_or_more
from awgbot.util.quorum.probe import Probe
from awgbot.util.quorum.timing import Timing

pytestmark = pytest.mark.unit

T = Timing()
EPOCH = 12
G = 4
T0 = 1000.0


def _voters(v: int) -> tuple[str, ...]:
    """Голосующие линии «a» о лидере «a»: кандидат «b» и шлюзы a1…a(v−1)."""
    return ("b",) + tuple(f"a{i}" for i in range(1, v))


def _leader(v: int, *, epoch: int = EPOCH, g: int = G, now: float = 0.0) -> Leader:
    ld = Leader(l="a", v=v, epoch=epoch, candidate="b", timing=T)
    ld.set_members(_voters(v), v, g, now)
    return ld


def _probe(ld: Leader, node: str, w: str, now: float, *, e: int | None = None, g: int | None = None, s: int = 1) -> str:
    return ld.probe_in(Probe("a", ld.gen.g if g is None else g, node, EPOCH if e is None else e, w, s), now)


def _lead(v: int) -> Leader:
    """Лидер, уже снявший стартовое ограждение (все видят в T0 − 100)."""
    ld = _leader(v)
    for n in ld.voters:
        _probe(ld, n, "see", T0 - 100)
    assert ld.tick(T0 - 100) == "unfence" and not ld.fenced
    return ld


# ── таблица истинности ───────────────────────────────────────────────────────

VIEWS = ("see", "sus", "uns", "held", None)        # None — молчит


@pytest.mark.parametrize("v", [2, 3, 4, 5])
def test_truth_table_of_the_leader(v):
    """Все сочетания взглядов голосующих (see/sus/uns/held/молчит), v = 2…5:
    лидер держится тогда и только тогда, когда «да» (только see) ≥ v/2; иначе
    через T_down — лишение кворума, если «да» кандидата не в счёте; с «да»
    кандидата (случай 3) — держится и на T_down, лишается через T_down_rf."""
    for combo in itertools.product(VIEWS, repeat=v):
        ld = _lead(v)
        now = T0
        for node, w in zip(ld.voters, combo):
            if w is not None:
                _probe(ld, node, w, now)
        yes = sum(1 for w in combo if w == "see")
        holds = half_or_more(yes, v)
        assert ld.yes_count(now) == yes, f"v={v} {combo}: счёт «да» {ld.yes_count(now)} вместо {yes}"
        r0 = ld.tick(now)
        r1 = ld.tick(now + T.down_s)
        case3 = combo[0] == "see"
        if holds or case3:
            assert (r0, r1) == ("", ""), f"v={v} {combo}: лидер не держится ({r0!r}, {r1!r})"
            assert not ld.fenced and ld.role() == "lead"
        else:
            assert (r0, r1) == ("", "fence"), f"v={v} {combo}: нет лишения через T_down ({r0!r}, {r1!r})"
            assert ld.fenced and ld.role() == "fenced"


@pytest.mark.parametrize("v", [3, 4, 5])
def test_case_three_holds_for_down_rf_while_the_candidate_keeps_seeing(v):
    """Случай 3: шлюзы не видят, кандидат видит и шлёт see — лидер держится 820 с
    (пользователям отвечает почти все 15 минут) и лишается кворума на 820-й."""
    ld = _lead(v)
    for n in ld.voters[1:]:
        _probe(ld, n, "uns", T0)
    fenced_at = None
    now = T0
    while now <= T0 + 1000:
        if (now - T0) % 15 == 0:
            _probe(ld, "b", "see", now)
        if ld.tick(now) == "fence":
            fenced_at = now
            break
        now += T.tick_s
    assert fenced_at == T0 + T.down_rf_s, f"лишение в {fenced_at}, ожидалось T0 + {T.down_rf_s}"


def test_case_three_switches_to_the_short_timer_when_the_candidate_stops_seeing():
    """Случай 3, затем «да» кандидата истекло (он перестал видеть в tb): лидер
    лишается кворума по T_down от начала, а не ждёт 820 с."""
    ld = _lead(3)
    for n in ("a1", "a2"):
        _probe(ld, n, "uns", T0)
    _probe(ld, "b", "see", T0)
    assert ld.tick(T0) == ""
    assert ld.tick(T0 + 20) == "", "с «да» кандидата в счёте лидер держится дольше T_down"
    _probe(ld, "b", "uns", T0 + 25)
    assert ld.tick(T0 + 25) == "fence", "кандидат перестал видеть — лишение сразу (T_down от начала прошёл)"


# ── что считается «да» ───────────────────────────────────────────────────────

def test_only_my_epoch_counts():
    ld = _lead(2)
    _probe(ld, "b", "see", T0, e=EPOCH - 1)
    _probe(ld, "a1", "see", T0, e=EPOCH)
    assert ld.yes_nodes(T0) == ("a1",), "проба со старой эпохой засчитана"


def test_a_probe_with_a_greater_epoch_is_adopt_and_not_a_yes():
    """Эпоха больше моей — эпоху держит другая сторона: `adopt`, в счёт не идёт."""
    ld = _lead(2)
    assert _probe(ld, "b", "see", T0, e=EPOCH + 1) == "adopt"
    assert _probe(ld, "a1", "see", T0, e=EPOCH) == ""
    assert ld.yes_nodes(T0) == ("a1",)


def test_a_foreign_line_or_node_is_ignored_and_not_recorded():
    ld = _lead(2)
    assert ld.probe_in(Probe("b", G, "a1", EPOCH, "see", 1), T0) == "ignore"
    assert ld.probe_in(Probe("a", G, "a3", EPOCH, "see", 1), T0) == "ignore"
    assert ld.probe_in(Probe("a", G, "b1", EPOCH, "see", 1), T0) == "ignore"
    assert ld.yes_count(T0) == 0


def test_a_probe_older_than_the_window_is_not_counted():
    ld = _lead(2)
    _probe(ld, "a1", "see", T0)
    assert ld.yes_count(T0 + T.yes_window_s) == 1, "проба ровно W назад ещё в окне"
    assert ld.yes_count(T0 + T.yes_window_s + 1) == 0, "проба старше W засчитана"


def test_the_last_probe_wins_a_see_followed_by_sus_is_not_a_yes():
    ld = _lead(2)
    _probe(ld, "a1", "see", T0)
    _probe(ld, "a1", "sus", T0 + 5)
    assert ld.yes_count(T0 + 5) == 0


def test_held_is_never_a_yes():
    ld = _lead(2)
    _probe(ld, "b", "held", T0)
    _probe(ld, "a1", "held", T0)
    assert ld.yes_count(T0) == 0


def test_the_previous_generation_counts_only_inside_the_window():
    """Смена состава g → g + 1 в T0: голос с g считается 120 с, после — нет; g − 1
    (на два поколения назад) — никогда."""
    ld = _lead(2)
    ld.set_members(_voters(2), 2, G + 1, T0)
    _probe(ld, "b", "see", T0 + 10, g=G)
    _probe(ld, "a1", "see", T0 + 10, g=G - 1)
    assert ld.yes_nodes(T0 + 10) == ("b",)
    _probe(ld, "b", "see", T0 + T.grace_s - 1, g=G)
    assert ld.yes_nodes(T0 + T.grace_s - 1) == ("b",)
    _probe(ld, "b", "see", T0 + T.grace_s, g=G)
    assert ld.yes_nodes(T0 + T.grace_s) == (), "голос прежнего поколения после окна засчитан"
    _probe(ld, "b", "see", T0 + T.grace_s, g=G + 1)
    assert ld.yes_nodes(T0 + T.grace_s) == ("b",)


def test_a_removed_voter_stops_counting_at_once():
    """Шлюз снят: его последняя проба `see` не держит лидера ни секунды."""
    ld = _lead(3)
    for n in ld.voters:
        _probe(ld, n, "see", T0)
    ld.set_members(("b", "a1"), 2, G + 1, T0 + 1)
    assert set(ld.yes_nodes(T0 + 1)) == {"b", "a1"}


# ── T_down ───────────────────────────────────────────────────────────────────

def test_fence_comes_after_down_seconds_of_continuous_minority():
    ld = _lead(3)
    _probe(ld, "b", "uns", T0)
    _probe(ld, "a1", "uns", T0)
    _probe(ld, "a2", "see", T0)
    assert ld.tick(T0) == ""
    assert ld.tick(T0 + T.down_s - 1) == ""
    assert ld.tick(T0 + T.down_s) == "fence"
    assert ld.tick(T0 + T.down_s + 5) == "", "лишение кворума повторилось"


def test_a_short_dip_restarts_the_down_timer():
    ld = _lead(3)
    _probe(ld, "b", "uns", T0)
    _probe(ld, "a1", "uns", T0)
    assert ld.tick(T0) == ""
    _probe(ld, "a1", "see", T0 + 15)
    _probe(ld, "a2", "see", T0 + 15)
    assert ld.tick(T0 + 15) == ""
    _probe(ld, "a1", "uns", T0 + 16)
    assert ld.tick(T0 + 16) == ""
    assert ld.tick(T0 + 25) == "", "провал не сбросил таймер T_down"
    assert ld.tick(T0 + 36) == "fence"


# ── старт и возврат ──────────────────────────────────────────────────────────

def test_the_leader_starts_without_the_right_to_write():
    ld = _leader(2)
    assert ld.fenced and ld.role() == "fenced"
    assert ld.tick(T0) == "", "на старте без «да» ограждение снято"


def test_the_first_majority_at_start_unfences_without_waiting_u():
    ld = _leader(3)
    _probe(ld, "b", "see", T0)
    assert ld.tick(T0) == ""
    _probe(ld, "a1", "see", T0 + 1)
    assert ld.tick(T0 + 1) == "unfence", "первое большинство на старте ждёт окно U"
    assert ld.role() == "lead"


def test_after_a_fence_the_return_needs_u_seconds_of_majority():
    ld = _lead(2)
    _probe(ld, "b", "uns", T0)
    _probe(ld, "a1", "uns", T0)
    ld.tick(T0)
    assert ld.tick(T0 + T.down_s) == "fence"
    t1 = T0 + 100
    _probe(ld, "b", "see", t1)
    assert ld.tick(t1) == ""
    assert ld.tick(t1 + T.unfence_s - 1) == "", "возврат раньше U"
    _probe(ld, "b", "see", t1 + 20)
    assert ld.tick(t1 + T.unfence_s) == "unfence"
    assert ld.tick(t1 + T.unfence_s + 5) == ""


def test_a_dip_during_u_restarts_the_return():
    ld = _lead(2)
    _probe(ld, "b", "uns", T0)
    _probe(ld, "a1", "uns", T0)
    ld.tick(T0)
    ld.tick(T0 + T.down_s)
    t1 = T0 + 100
    _probe(ld, "b", "see", t1)
    ld.tick(t1)
    _probe(ld, "b", "sus", t1 + 10)
    assert ld.tick(t1 + 10) == ""
    _probe(ld, "b", "see", t1 + 15)
    assert ld.tick(t1 + 15) == ""
    assert ld.tick(t1 + 40) == "", "U отсчитано от первого «да», а не от последнего провала"
    assert ld.tick(t1 + 45) == "unfence"


def test_held_votes_do_not_bring_a_fenced_leader_back():
    """Сеть восстановилась, но голосующие держат обязательство «нет»: `held` —
    лидер остаётся без права записи сколько угодно."""
    ld = _lead(3)
    for n in ld.voters:
        _probe(ld, n, "uns", T0)
    ld.tick(T0)
    assert ld.tick(T0 + T.down_s) == "fence"
    now = T0 + 30
    while now < T0 + 600:
        for n in ("a1", "a2"):
            _probe(ld, n, "held", now)
        _probe(ld, "b", "see", now, e=EPOCH + 1)
        assert ld.tick(now) == "", f"возврат по held в {now - T0}"
        now += 5
    assert ld.fenced


def test_fence_now_restarts_the_count():
    ld = _lead(2)
    _probe(ld, "b", "see", T0)
    ld.fence_now(T0)
    assert ld.fenced
    assert ld.tick(T0 + 1) == "", "после ограждения извне возврат без окна U"


# ── ответ ────────────────────────────────────────────────────────────────────

def test_reply_carries_role_count_generation_and_epoch():
    ld = _leader(3)
    p = Probe("a", G, "a1", EPOCH, "see", 777)
    ld.probe_in(p, T0)
    r = ld.reply_for(p, T0)
    assert (r.l, r.g, r.e, r.r, r.s, r.y, r.v) == ("a", G, EPOCH, "fenced", 777, 1, 3)
    _probe(ld, "b", "see", T0)
    ld.tick(T0)
    r = ld.reply_for(p, T0)
    assert (r.r, r.y) == ("lead", 2)
