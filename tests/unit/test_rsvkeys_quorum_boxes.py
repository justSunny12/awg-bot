"""Ключ кворума `Mq`, аварийный ключ `Mc` и два ящика вокруг `M`
(util/rsvkeys.py: QuorumGen, CodeGen, seal_box/open_box, recover_m_quorum,
recover_m_code; концепт «кворум» §2.7).

Цена ошибки: ящик, который не открывается t точками большинства, — переезд по
кворуму в аварию срывается на последнем шаге; ящик, который открывается меньше
чем t точками или чужими, — сторона резерва в одиночку достаёт `M` и ключ
пакета; аварийный код, который не открывает ящик кода (или открывает его
с опечаткой), — у админа нет последнего рычага или он не знает, что ошибся.
"""
from __future__ import annotations

import itertools

import pytest

from awgbot.util import rsvkeys, shamir

pytestmark = pytest.mark.unit

M = bytes(range(16))            # DUMMY: главный ключ для тестов


def _holders(qg: rsvkeys.QuorumGen, slots) -> list[shamir.Point]:
    return [qg.reserve_point()] + [qg.gateway_point(s) for s in slots]


# ── ящик кворума ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("t,slots", [(2, [1]), (2, [1, 2]), (3, [1, 2, 3]), (3, [1, 2, 3, 4]), (4, [1, 2, 3, 4, 5])])
def test_any_t_holders_open_the_quorum_box(t, slots):
    """Второй сервер (x = 1) и шлюзы основы (x = слот + 1): любые t из них — `M`."""
    qg = rsvkeys.QuorumGen.new(5, t)
    assert (qg.q_gen, qg.t) == (5, t)
    box = qg.qbox(M)
    pts = _holders(qg, slots)
    assert [p[0] for p in pts] == [1] + [s + 1 for s in slots], "точки сидят не на тех x"
    for combo in itertools.combinations(pts, t):
        assert rsvkeys.recover_m_quorum(list(combo), box) == M, f"точки {[p[0] for p in combo]} не открыли ящик"


@pytest.mark.parametrize("t", [2, 3, 4])
def test_fewer_than_t_points_do_not_open_the_quorum_box(t):
    """t − 1 точек (сторона резерва и меньшинство шлюзов) — ValueError: при t = 2
    одна точка не многочлен вовсе, при t ≥ 3 прямая по двум точкам даёт не `Mq`."""
    qg = rsvkeys.QuorumGen.new(1, t)
    box = qg.qbox(M)
    pts = _holders(qg, range(1, t + 2))
    for combo in itertools.combinations(pts, t - 1):
        with pytest.raises(ValueError):
            rsvkeys.recover_m_quorum(list(combo), box)


def test_points_of_another_quorum_generation_do_not_open_the_box():
    """Смена состава даёт новый многочлен: точки прежнего (у снятого шлюза) с
    новым ящиком — отказ, и новые точки к прежнему ящику — тоже."""
    old, new = rsvkeys.QuorumGen.new(1, 2), rsvkeys.QuorumGen.new(2, 2)
    old_box, new_box = old.qbox(M), new.qbox(M)
    with pytest.raises(ValueError):
        rsvkeys.recover_m_quorum([old.reserve_point(), old.gateway_point(1)], new_box)
    with pytest.raises(ValueError):
        rsvkeys.recover_m_quorum([new.reserve_point(), new.gateway_point(1)], old_box)
    with pytest.raises(ValueError):
        rsvkeys.recover_m_quorum([new.reserve_point(), old.gateway_point(1)], new_box)


def test_points_of_the_wrong_length_are_refused():
    qg = rsvkeys.QuorumGen.new(1, 2)
    box = qg.qbox(M)
    with pytest.raises(ValueError):
        rsvkeys.recover_m_quorum([(1, b"\x00" * 15), (2, b"\x00" * 15)], box)


def test_a_damaged_box_is_refused():
    qg = rsvkeys.QuorumGen.new(1, 2)
    box = bytearray(qg.qbox(M))
    box[30] ^= 1
    with pytest.raises(ValueError):
        rsvkeys.recover_m_quorum([qg.reserve_point(), qg.gateway_point(1)], bytes(box))
    with pytest.raises(ValueError):
        rsvkeys.recover_m_quorum([qg.reserve_point(), qg.gateway_point(1)], b"")


# ── ящик кода ────────────────────────────────────────────────────────────────

def test_the_code_with_the_reserve_point_opens_the_code_box():
    cg = rsvkeys.CodeGen.new(3)
    assert cg.c_gen == 3
    box = cg.cbox(M)
    assert rsvkeys.recover_m_code(cg.code(), cg.reserve_point(), box) == M
    # руки: нижний регистр и пробелы вместо дефисов прощаются
    assert rsvkeys.recover_m_code(cg.code().lower().replace("-", " "), cg.reserve_point(), box) == M


def test_a_mistyped_code_is_a_value_error_not_a_wrong_key():
    """Один знак кода не тот — контрольная сумма ловит: админ видит «не сошёлся»,
    а не тихо собранный чужой ключ."""
    cg = rsvkeys.CodeGen.new(1)
    box = cg.cbox(M)
    code = cg.code()
    bad = ("1" if code[0] != "1" else "2") + code[1:]
    with pytest.raises(ValueError):
        rsvkeys.recover_m_code(bad, cg.reserve_point(), box)
    with pytest.raises(ValueError):
        rsvkeys.recover_m_code("ABCD", cg.reserve_point(), box)


def test_the_code_of_another_line_or_a_quorum_point_does_not_open_the_code_box():
    """Код прежнего поколения `Mc`, точка многочлена кворума вместо точки прямой
    кода — отказ: ящик кода открывается только своей прямой."""
    cg, other = rsvkeys.CodeGen.new(1), rsvkeys.CodeGen.new(2)
    box = cg.cbox(M)
    with pytest.raises(ValueError):
        rsvkeys.recover_m_code(other.code(), cg.reserve_point(), box)
    with pytest.raises(ValueError):
        rsvkeys.recover_m_code(cg.code(), other.reserve_point(), box)
    qg = rsvkeys.QuorumGen.new(1, 2)
    with pytest.raises(ValueError):
        rsvkeys.recover_m_code(cg.code(), qg.reserve_point(), box)
    with pytest.raises(ValueError):
        rsvkeys.recover_m_code(cg.code(), (1, b"\x00" * 8), box)


def test_the_two_boxes_are_not_interchangeable():
    """Ящик кворума ключом кода и ящик кода ключом кворума не открываются, даже
    если байты `Mq` и `Mc` совпали: ключи ящиков разведены разной персонализацией."""
    same = bytes(range(32, 48))
    assert rsvkeys.qbox_key(same) != rsvkeys.cbox_key(same)
    assert rsvkeys.qbox_key(same) != rsvkeys.box_key(same), "ключ ящика кворума совпал с ключом пакета"
    qbox = rsvkeys.seal_box(rsvkeys.qbox_key(same), M)
    cbox = rsvkeys.seal_box(rsvkeys.cbox_key(same), M)
    with pytest.raises(ValueError):
        rsvkeys.open_box(rsvkeys.cbox_key(same), qbox)
    with pytest.raises(ValueError):
        rsvkeys.open_box(rsvkeys.qbox_key(same), cbox)
    assert rsvkeys.open_box(rsvkeys.qbox_key(same), qbox) == M


# ── форма ────────────────────────────────────────────────────────────────────

def test_a_box_is_nonce_m_and_mac_and_two_seals_differ():
    """Ящик — 24 + 16 + 16 = 56 байт (место в `qkeys` и `server_state` под это);
    нонс случайный: два ящика одного `M` разные."""
    qg, cg = rsvkeys.QuorumGen.new(1, 2), rsvkeys.CodeGen.new(1)
    a, b, c = qg.qbox(M), qg.qbox(M), cg.cbox(M)
    assert rsvkeys.BOX_LEN == 56
    assert len(a) == len(b) == len(c) == rsvkeys.BOX_LEN
    assert a != b, "нонс ящика не случаен"


def test_the_box_holds_only_a_16_byte_master():
    key = rsvkeys.qbox_key(M)
    with pytest.raises(ValueError):
        rsvkeys.seal_box(key, b"\x01" * 15)
    with pytest.raises(ValueError):
        rsvkeys.qbox_key(b"\x01" * 15)
    with pytest.raises(ValueError):
        rsvkeys.cbox_key(b"")


def test_keys_are_fresh_per_generation_and_not_m():
    q1, q2 = rsvkeys.QuorumGen.new(1, 3), rsvkeys.QuorumGen.new(1, 3)
    c1, c2 = rsvkeys.CodeGen.new(1), rsvkeys.CodeGen.new(1)
    assert len(q1.mq) == len(c1.mc) == rsvkeys.M_LEN
    assert q1.mq != q2.mq and c1.mc != c2.mc
    assert c1.code() != c2.code()
    assert rsvkeys.code_point(c1.code()) == c1.line.point(rsvkeys.CODE_X)
