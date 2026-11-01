"""Отпечаток многочлена ключа кворума у шлюза (rsvkeys.q_print, `point.qf` раздела
`quorum`): после восстановления основы из копии номера многочленов `q_gen` идут
заново, и точка прежнего многочлена совпадает с новым по номеру. Шлюз несёт отпечаток
рядом с номером: в документе (gwcfg doc/check; отпечаток раздела — без него), в
поле `quo` снимка (`qf`, `pg.qf`), в `point_given` quorum.json и в `qshare`; на
`qneed` отдаёт точку, только если совпали номер и отпечаток (отпечатка нет у одной
стороны — по номеру, как прежде).

Цена ошибки: точка прежнего многочлена, отданная под тем же номером, — кандидат
собирает «ключ», который ящик кворума не открывает, и переезд по кворуму срывается
в аварии; основа, видящая в снимке номер без отпечатка, считает точки на месте, хотя
у шлюзов прежний многочлен; отпечаток в отпечатке раздела — агент прежней версии его
не хранит, документ уходил бы ему снова и снова.
"""
from __future__ import annotations

import pytest

from awgbot.domain import gwcfg, gwsnapshot
from awgbot.domain.gwcfg import check as gwcheck
from awgbot.util import pairproto, rsvkeys

from tests.unit.test_gw_quorum_decision import POINT_Y, Q_GEN, ag, clock

pytestmark = pytest.mark.unit
__all__ = ["ag", "clock"]

QF_A = rsvkeys.q_print(bytes([1]) * rsvkeys.M_LEN)        # многочлен до восстановления основы из копии
QF_B = rsvkeys.q_print(bytes([2]) * rsvkeys.M_LEN)        # после — под тем же номером
NEW_Y = "ef" * 16


def _apply(ag, point: dict) -> None:
    sec = ag.section()
    doc = {k: sec[k] for k in ("l", "g", "members", "t", "port", "probes", "e")}
    ag.svc.quorum_apply({**doc, "point": point})


@pytest.fixture()
def held(ag):
    """Шлюз держит точку многочлена QF_A поколения Q_GEN."""
    _apply(ag, {"x": 2, "y": POINT_Y, "q_gen": Q_GEN, "qf": QF_A})
    return ag


# ── отпечаток ───────────────────────────────────────────────────────────────

def test_the_print_tells_two_polynomials_apart_and_is_not_the_secret():
    mq = bytes([1]) * rsvkeys.M_LEN
    assert len(QF_A) == 16 and rsvkeys.m_print_of(QF_A) == QF_A and QF_A != QF_B
    assert QF_A != rsvkeys.m_print(mq), "отпечаток многочлена совпал с отпечатком M того же значения"
    assert mq.hex()[:16] != QF_A
    with pytest.raises(ValueError):
        rsvkeys.q_print(b"short")


@pytest.mark.parametrize("a, b, same", [
    ({"q_gen": 3, "qf": QF_A}, {"q_gen": 3, "qf": QF_A}, True),
    ({"q_gen": 3, "qf": QF_A}, {"q_gen": 3, "qf": QF_B}, False),
    ({"q_gen": 3, "qf": QF_A}, {"q_gen": 4, "qf": QF_A}, False),
    ({"q_gen": 3, "qf": QF_A}, {"q_gen": 3}, True),
    ({"q_gen": 3, "qf": "zz"}, {"q_gen": 3, "qf": QF_B}, True),
], ids=["same", "same_number_other_poly", "other_number", "no_print_one_side", "garbled_print"])
def test_one_polynomial_is_the_number_and_the_print_when_both_have_it(a, b, same):
    assert rsvkeys.same_q(a, b) is same


@pytest.mark.parametrize("kind", ["qneed", "qshare"])
def test_the_print_is_an_optional_field_of_the_guest_words(kind):
    sender = pairproto.RESERVE if kind == "qneed" else pairproto.GATEWAY
    body = {"l": "a", "q_gen": 4, "since": "x"} if kind == "qneed" else {"l": "a", "q_gen": 4, "x": 2, "y": POINT_Y}
    assert pairproto.check(pairproto.GUEST_PROTO, kind, dict(body), sender) == body, "слово шлюза прежней версии"
    assert pairproto.check(pairproto.GUEST_PROTO, kind, {**body, "qf": QF_A}, sender)["qf"] == QF_A
    with pytest.raises(pairproto.ProtocolError):
        pairproto.check(pairproto.GUEST_PROTO, kind, {**body, "qf": "ab" * 9}, sender)


# ── qneed → qshare ──────────────────────────────────────────────────────────

def test_a_need_of_the_same_polynomial_gets_the_point_with_its_print(held):
    held.uns()
    acts = held.word("qneed", q_gen=Q_GEN, qf=QF_A, since="2026-10-30T10:05:00+03:00")
    assert acts == [("guest", "qshare", {"l": "a", "q_gen": Q_GEN, "x": 2, "y": POINT_Y, "qf": QF_A})], acts
    assert held.section()["point_given"]["qf"] == QF_A, "point_given без отпечатка — основа сверит только номер"


def test_a_need_of_another_polynomial_under_the_same_number_is_refused(held):
    held.uns()
    assert held.word("qneed", q_gen=Q_GEN, qf=QF_B, since="x") == [], "точка прежнего многочлена отдана под тем же номером"
    assert "point_given" not in held.section()


def test_a_need_without_the_print_is_answered_by_the_number(held):
    """Резерв прежней версии отпечатка не шлёт — точка по номеру, как раньше."""
    held.uns()
    assert held.word("qneed", q_gen=Q_GEN, since="x")[0][2]["y"] == POINT_Y


def test_a_point_from_an_older_server_answers_without_a_print(ag):
    """Документ сервера прежней версии — точка без отпечатка: qshare и point_given без него."""
    ag.uns()
    acts = ag.word("qneed", q_gen=Q_GEN, qf=QF_A, since="x")
    assert acts and "qf" not in acts[0][2], acts
    assert "qf" not in ag.section()["point_given"]


def test_a_new_polynomial_under_the_same_number_keeps_the_old_point_as_previous(held):
    """Основа восстановлена из копии: тот же номер, другой многочлен — прежняя точка
    живёт как `point_prev`, кандидат с ящиком прежнего получает её, с новым — новую."""
    _apply(held, {"x": 2, "y": NEW_Y, "q_gen": Q_GEN, "qf": QF_B})
    assert held.section()["point_prev"]["qf"] == QF_A, "точка прежнего многочлена под тем же номером затёрта"
    held.uns()
    assert held.word("qneed", q_gen=Q_GEN, qf=QF_A, since="x")[0][2]["y"] == POINT_Y
    assert held.word("qneed", q_gen=Q_GEN, qf=QF_B, since="x")[0][2]["y"] == NEW_Y


def test_the_same_point_once_with_the_print_is_not_a_new_generation(ag):
    """Сервер обновился: тот же многочлен теперь с отпечатком — прежней точкой он не становится."""
    _apply(ag, {"x": 2, "y": POINT_Y, "q_gen": Q_GEN, "qf": QF_A})
    assert "point_prev" not in ag.section()


# ── снимок ──────────────────────────────────────────────────────────────────

def test_the_snapshot_carries_the_print_and_the_server_takes_only_a_print(held):
    held.uns()
    held.word("qneed", q_gen=Q_GEN, qf=QF_A, since="x")
    quo = held.svc.quorum_snapshot()
    assert quo["q"] == Q_GEN and quo["qf"] == QF_A and quo["pg"]["qf"] == QF_A, quo
    clean = gwsnapshot.sanitize({"quo": quo})["quo"]
    assert clean["qf"] == QF_A and clean["pg"]["qf"] == QF_A, clean
    bad = gwsnapshot.sanitize({"quo": {**quo, "qf": "x; rm", "pg": {"q": 3, "qf": ["a"]}}})["quo"]
    assert "qf" not in bad and "qf" not in bad["pg"], bad


def test_a_snapshot_without_a_print_has_no_qf(ag):
    assert "qf" not in ag.svc.quorum_snapshot()
    assert "qf" not in gwsnapshot.sanitize({"quo": {"w": "see", "g": 1, "e": 0, "q": 3}})["quo"]


# ── документ ────────────────────────────────────────────────────────────────

def _sec(point: dict) -> dict:
    from tests.unit.test_gw_quorum_decision import CONF
    return {"l": "a", "g": 1, "members": ["a", "b", "a1"], "t": 2, "port": 8789, "probes": {"h": CONF}, "e": 1,
            "point": point}


def test_the_document_carries_the_print_of_the_point():
    msg = gwcfg.build(3, "ab" * 8, {"quorum": _sec({"x": 2, "y": POINT_Y, "q_gen": Q_GEN, "qf": QF_A})})
    point = gwcfg.parse(msg)["sections"]["quorum"]["point"]
    assert point["qf"] == QF_A, point
    assert gwcheck.check_quorum(gwcfg.parse(msg)["sections"]["quorum"])["point"]["qf"] == QF_A
    old = gwcfg.parse(gwcfg.build(3, "ab" * 8, {"quorum": _sec({"x": 2, "y": POINT_Y, "q_gen": Q_GEN})}))
    assert "qf" not in old["sections"]["quorum"]["point"], "отпечаток выдуман за сервер прежней версии"


def test_a_point_without_the_print_passes_as_before():
    assert gwcheck.check_quorum(_sec({"x": 2, "y": POINT_Y, "q_gen": Q_GEN}))["point"] == {"x": 2, "y": POINT_Y,
                                                                                          "q_gen": Q_GEN}


def test_a_garbled_print_refuses_the_section():
    with pytest.raises(gwcfg.CfgError):
        gwcheck.check_quorum(_sec({"x": 2, "y": POINT_Y, "q_gen": Q_GEN, "qf": "x" * 16}))


def test_the_print_of_the_section_does_not_depend_on_the_print_of_the_point():
    """Агент прежней версии отпечаток точки не хранит: в отпечатке раздела его нет — иначе
    отпечатки разошлись бы навсегда."""
    with_qf = gwcfg.quorum_print(_sec({"x": 2, "y": POINT_Y, "q_gen": Q_GEN, "qf": QF_A}))
    assert with_qf and with_qf == gwcfg.quorum_print(_sec({"x": 2, "y": POINT_Y, "q_gen": Q_GEN}))
    assert with_qf != gwcfg.quorum_print(_sec({"x": 2, "y": NEW_Y, "q_gen": Q_GEN})), "новый многочлен не меняет отпечаток"
