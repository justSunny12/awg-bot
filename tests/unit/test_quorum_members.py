"""Состав линии, поколение, порог и окно прежнего поколения
(util/quorum/members.py; концепт «кворум» §2.1–§2.3).

Цена ошибки: шлюз чужой стороны в составе — голосует о чужом сервере (К5) и
держит точку чужого ключа; состав без одного из серверов — порог считается не
от того ансамбля; поколение, которое не растёт на смене, — голоса снятого шлюза
продолжают считаться; окно прежнего поколения, которое не закрывается, — то же;
состав, вступивший в силу без подтверждения второго сервера, — основа меняет
ансамбль в одиночку, чтобы набрать большинство.
"""
from __future__ import annotations

import pytest

from awgbot.util.quorum import members as mm
from awgbot.util.quorum.members import Composition, GenWindow, Members

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


def test_adding_and_removing_a_gateway_bumps_the_generation():
    m = Members.make("a", 3, ["a", "b", "a1"])
    m2 = m.with_gateway("a2")
    assert (m2.g, m2.members) == (4, ("a", "b", "a1", "a2"))
    m3 = m2.without_gateway("a1")
    assert (m3.g, m3.members) == (5, ("a", "b", "a2"))
    assert m.g == 3, "прежний состав неизменен"


def test_no_op_changes_keep_the_generation():
    """Повтор того же объявления `quo1` и попытка снять сервер — поколение не растёт."""
    m = Members.make("a", 3, ["a", "b", "a1"])
    assert m.with_gateway("a1") == m
    assert m.without_gateway("a2") == m
    assert m.without_gateway("b") == m
    assert m.without_gateway("a") == m


def test_adding_a_gateway_of_the_other_side_is_refused():
    with pytest.raises(ValueError):
        Members.make("a", 1, ["a", "b"]).with_gateway("b1")


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


# ── подтверждение вторым сервером ───────────────────────────────────────────

def test_a_new_composition_acts_only_after_the_second_server_acks():
    m1 = Members.make("a", 1, ["a", "b", "a1"])
    c = Composition(mine=m1)
    assert c.pending() and c.active() is None, "первое поколение без подтверждения действует"
    assert c.ack(1) and not c.pending() and c.active() == m1
    m2 = m1.with_gateway("a2")
    c.propose(m2)
    assert c.pending() and c.active() == m1, "пока подтверждения нет, действует прежнее поколение"
    assert not c.ack(3) and c.active() == m1, "подтверждено поколение, которого не предлагали"
    assert c.ack(2) and c.active() == m2 and not c.pending()


def test_a_stale_ack_after_a_second_proposal_is_ignored():
    """Ответ `qmem_ok` на g = 2 пришёл, когда основа уже предложила g = 3, —
    действует прежнее подтверждённое, а не g = 2 и не g = 3."""
    m1 = Members.make("a", 1, ["a", "b", "a1"])
    c = Composition(mine=m1)
    c.ack(1)
    m2 = m1.with_gateway("a2")
    c.propose(m2)
    m3 = m2.with_gateway("a3")
    c.propose(m3)
    assert not c.ack(2)
    assert c.active() == m1


def test_a_composition_restored_with_an_ack_keeps_acting_while_the_next_is_pending():
    """Состав, поднятый с диска уже подтверждённым (`acked` = g), при следующем
    предложении остаётся действующим до `qmem_ok` — иначе кворум линии на эти
    секунды гаснет и смена состава выглядит как выключение."""
    m1 = Members.make("a", 4, ["a", "b", "a1"])
    c = Composition(mine=m1, acked=4)
    assert c.active() == m1
    c.propose(m1.with_gateway("a2"))
    assert c.active() == m1, "действующий состав пропал, пока новое поколение ждёт подтверждения"


# ── причины выключения ───────────────────────────────────────────────────────

def test_off_reasons_is_empty_when_everything_holds():
    m = Members.make("a", 1, ["a", "b", "a1"])
    assert mm.off_reasons(m, peer_quo1=True, acked=True) == []


def test_off_reasons_names_every_problem():
    got = mm.off_reasons(Members.make("a", 1, ["a", "b"]), peer_quo1=False, acked=False, timing_problem="up",
                         overlaps=["10.0.0.0/8"], big_slots=[60], keys_ok=False)
    assert got == ["peer_old", "no_gateways", "not_acked", "keys", "timing:up", "overlap:10.0.0.0/8", "slot:60"]
    assert mm.off_reasons(None, peer_quo1=True, acked=True) == ["no_gateways"]
