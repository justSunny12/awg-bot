"""Состав линии, поколение, порог и окно прежнего поколения
(util/quorum/members.py; концепт «кворум» §2.1–§2.3).

Цена ошибки: шлюз чужой стороны в составе — голосует о чужом сервере (К5) и
держит точку чужого ключа; состав без одного из серверов — порог считается не
от того ансамбля; окно прежнего поколения, которое не закрывается, — голоса
снятого шлюза продолжают считаться. Смена поколения и подтверждение состава
вторым сервером — у домена, tests/integration/test_reserve_quorum_members.
"""
from __future__ import annotations

import pytest

from awgbot.util.quorum import members as mm
from awgbot.util.quorum.members import GenWindow, Members

pytestmark = pytest.mark.unit


# ── узлы ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("node,want", [("a", ("a", 0)), ("b", ("b", 0)), ("a1", ("a", 1)), ("b58", ("b", 58))])
def test_parse_node_reads_servers_and_gateways(node, want):
    assert mm.parse_node(node) == want


@pytest.mark.parametrize("node", ["", "c", "a0", "a59", "a01", "A1", "a-1", "ab", None, "a1 "])
def test_parse_node_refuses_garbage_and_slots_beyond_the_block(node):
    with pytest.raises(ValueError):
        mm.parse_node(node)


# ── состав ───────────────────────────────────────────────────────────────────

def test_a_composition_with_a_gateway_of_the_other_side_is_refused():
    """Шлюз «Beta» о линии «Alpha» не голосует (К5)."""
    with pytest.raises(ValueError):
        Members.make("a", 1, ["a", "b", "a1", "b1"])
    with pytest.raises(ValueError):
        Members.make("b", 1, ["b", "a", "a1"])


@pytest.mark.parametrize("nodes", [["a", "a1"], ["b", "a1"], ["a1", "a2"], []])
def test_a_composition_without_both_servers_is_refused(nodes):
    with pytest.raises(ValueError):
        Members.make("a", 1, nodes)


def test_a_node_twice_or_a_bad_generation_or_line_is_refused():
    with pytest.raises(ValueError):
        Members.make("a", 1, ["a", "b", "a1", "a1"])
    with pytest.raises(ValueError):
        Members.make("a", 0, ["a", "b", "a1"])
    with pytest.raises(ValueError):
        Members.make("c", 1, ["a", "b"])


def test_voters_follow_the_concept_table():
    """Линия «Alpha», n = 4: штатно лидер «Alpha» — голосуют «Beta», Шлюз 1,
    Шлюз 2; в отъезде лидер — бот «Alpha» на «Beta», голосуют «Alpha» и шлюзы."""
    m = Members.make("a", 4, ["a", "b", "a1", "a2"])
    assert m.n == 4 and m.gateways == ("a1", "a2")
    assert m.voters("a") == ("b", "a1", "a2")
    assert m.voters("b") == ("a", "a1", "a2")
    assert (m.v("a"), m.t("a")) == (3, 2)
    with pytest.raises(ValueError):
        m.voters("a3")


@pytest.mark.parametrize("v,t", [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (6, 4)])
def test_threshold_is_a_strict_majority(v, t):
    assert mm.threshold(v) == t
    assert mm.majority(t, v) and not mm.majority(t - 1, v)


@pytest.mark.parametrize("k,v,maj,half", [
    (1, 2, False, True), (2, 2, True, True), (0, 2, False, False),
    (1, 3, False, False), (2, 3, True, True),
    (2, 4, False, True), (3, 4, True, True), (1, 4, False, False),
    (2, 5, False, False), (3, 5, True, True),
])
def test_majority_and_half_or_more(k, v, maj, half):
    """«Нет» — строго больше половины; «да» лидера — не меньше половины. При чётном
    v ровно половина — лидер держится, кандидат не забирает (ничья)."""
    assert mm.majority(k, v) is maj
    assert mm.half_or_more(k, v) is half


def test_quorum_needs_at_least_one_gateway():
    """v = 1 (только серверы) — кворум не включается (§12, решение 1)."""
    assert not Members.make("a", 1, ["a", "b"]).enabled
    assert Members.make("a", 1, ["a", "b", "a1"]).enabled


@pytest.mark.parametrize("nodes", [["a", "b", "b1"], ["a", "b", "a1", "a1"]], ids=["чужая сторона", "дважды"])
def test_a_gateway_of_the_other_side_or_a_repeated_node_is_refused(nodes):
    """Состав, присланный в `qmem` или поднятый с диска, с шлюзом другой стороны или
    узлом дважды не собирается: голос чужого шлюза о чужом сервере (К5)."""
    with pytest.raises(ValueError):
        Members.make("a", 1, nodes)


def test_dict_round_trip():
    m = Members.make("b", 7, ["b", "a", "b2"])
    assert Members.from_dict(m.to_dict()) == m
    with pytest.raises(ValueError):
        Members.from_dict({})


# ── окно прежнего поколения ──────────────────────────────────────────────────

def test_the_previous_generation_counts_until_the_window_ends():
    """После смены состава голос с g − 1 считается 120 с (документ шлюзам идёт с
    задержкой), после окна — нет; g − 2 — никогда."""
    w = GenWindow()
    w.bump(1, 0.0, 120)
    assert w.accepts(1, 0.0) and not w.accepts(0, 0.0), "поколения 0 нет — первое окно пусто"
    w.bump(2, 1000.0, 120)
    assert w.accepts(2, 1000.0)
    assert w.accepts(1, 1000.0) and w.accepts(1, 1119.9)
    assert not w.accepts(1, 1120.0), "окно прежнего поколения не закрылось"
    w.bump(3, 1200.0, 120)
    assert w.accepts(2, 1200.0) and not w.accepts(1, 1200.0), "g − 2 считается"
    assert not w.accepts(4, 1200.0)


def test_repeating_the_same_generation_does_not_reopen_the_window():
    w = GenWindow()
    w.bump(1, 0.0, 120)
    w.bump(2, 10.0, 120)
    w.bump(2, 500.0, 120)
    assert not w.accepts(1, 200.0), "повтор того же g продлил окно прежнего"


def test_two_quick_changes_close_the_older_window():
    w = GenWindow()
    w.bump(1, 0.0, 120)
    w.bump(2, 10.0, 120)
    w.bump(3, 20.0, 120)
    assert w.accepts(2, 30.0) and not w.accepts(1, 30.0)


# ── причины выключения ───────────────────────────────────────────────────────

def test_off_reasons_is_empty_when_everything_holds():
    m = Members.make("a", 1, ["a", "b", "a1"])
    assert mm.off_reasons(m, acked=True) == []


def test_two_servers_without_gateways_are_a_line_with_v_1_not_an_off_reason():
    """Без шлюзов линия всё равно включается — голосует только второй сервер:
    «нет шлюзов» — признак v < 2 (решений нет), а не причина «не включено»."""
    m = Members.make("a", 1, ["a", "b"])
    assert mm.off_reasons(m, acked=True) == [], mm.off_reasons(m, acked=True)
    assert not m.enabled and m.v("a") == 1, "состав из двух серверов — не v = 1 без решений"


def test_off_reasons_names_every_problem():
    got = mm.off_reasons(Members.make("a", 1, ["a", "b"]), acked=False, timing_problem="up",
                         overlaps=["10.0.0.0/8"], big_slots=[60], keys_ok=False)
    assert got == ["not_acked", "keys", "timing:up", "overlap:10.0.0.0/8", "slot:60"], got
    assert "peer_old" not in got, "причина «второй сервер 4.1.x» осталась"
    assert mm.off_reasons(None, acked=True) == ["no_gateways"], "без состава — причина «голосующих нет»"

