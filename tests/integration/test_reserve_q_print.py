"""Отпечаток многочлена ключа кворума рядом с номером `q_gen` у основы и резерва
(util/rsvkeys.q_print, quorum/keys.py, quorum/inbound.py, quorum/candidate.py): после
восстановления основы из копии `rsv_qkeys` в копию не идут — номера многочленов идут
заново и совпадают с выданными до копии. Сверки по одному номеру:

- основа считала точки шлюзов на месте (`quo.q` снимка) и `qkeys_ok` второго сервера
  подтверждением, хотя у них прежний многочлен под тем же номером;
- сигнал шлюза об отданной точке (`quo.pg`) решался номером: точка многочлена чужого
  `M` с номером не ниже первого многочлена текущего — отметка «ключ известен»;
- кандидат на резерве принимал `qshare` точки прежнего многочлена того же номера.

Совместимость: поле `qf` необязательное в `qkeys`, `qkeys_ok`, `qneed`, `qshare`, в точке
документа и в снимке; шлюз или сервер прежней версии его не шлёт — сверка по номеру.

Миры: линия «Alpha» (`line`, tests/integration/_reserve_quorum.py) — сторона основы;
резерв с пакетом «Beta» (`box`, `cand`, tests/integration/_reserve_vote_common.py);
основа с переездом (`mv`, tests/integration/_reserve_move.py) — сигнал шлюза.

Цена ошибки: кворум линии «включён», а точки у шлюзов от прежнего многочлена — переезд
по кворуму в аварии не открывает ящик; кандидат собирает точки разных многочленов и
срывает открытие; ложная отметка «ключ известен» — лишняя смена поколения, пропущенная
— ключ, который второй сервер мог собрать, остаётся в ходу.
"""
from __future__ import annotations

import pytest

from awgbot.util import pairproto, rsvkeys, shamir

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_quorum import gens, line
from tests.integration._reserve_vote_common import (GW1_Y, LINE, M_GEN, Q_GEN, QBOX, RSV_Y, box, cand, gw_sent,
                                                    sent)

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["line", "box", "cand", "mv"]

QF_A = rsvkeys.q_print(bytes([1]) * rsvkeys.M_LEN)        # многочлен до восстановления из копии
QF_B = rsvkeys.q_print(bytes([2]) * rsvkeys.M_LEN)        # после — под тем же номером


def _qf(s) -> str:
    return s._quorum_qf()


def _on(line) -> None:
    """Линия включена: шлюз 1 в составе, многочлен есть, «Beta» и шлюз ключи приняли."""
    line.add_gw(1)
    line.enable()
    assert _qf(line.s), "подготовка: многочлена нет"


# ── канал пары ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["qkeys", "qkeys_ok"])
def test_the_print_is_an_optional_field_of_the_pair_words(kind):
    assert "qf" in pairproto.PAIR_KINDS[kind][1] and "qf" in pairproto.OPTIONAL[kind]


# ── основа ──────────────────────────────────────────────────────────────────

def test_the_point_of_the_gateway_and_qkeys_carry_the_print_of_the_polynomial(line):
    _on(line)
    q = line.s.reserve_qkeys()
    want = rsvkeys.q_print(shamir.Poly(tuple(bytes.fromhex(c) for c in q["poly"])).secret)
    assert _qf(line.s) == want
    assert line.s.reserve_quorum_gw_point(1)["qf"] == want, "точка шлюзу без отпечатка многочлена"
    line.s.reserve_qkeys_reset()
    line.advance(120)
    body = line.s.reserve_qkeys_body()
    assert body is not None and body["qf"] == want, body
    pairproto.check(pairproto.PAIR_PROTO, "qkeys", dict(body), pairproto.INVITER)


def test_a_gateway_with_a_point_of_another_polynomial_under_the_number_is_not_ready(line):
    _on(line)
    assert line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed())
    line.snaps[1] = {"quo": {"w": "see", "g": 1, "e": 0, "q": gens(line.s)["q_gen"], "qf": QF_A}}
    assert QF_A != _qf(line.s)
    assert not line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed()), \
        "точка прежнего многочлена того же номера сочтена на месте"
    line.snaps[1] = {"quo": {"w": "see", "g": 1, "e": 0, "q": gens(line.s)["q_gen"], "qf": _qf(line.s)}}
    assert line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed())
    line.snaps[1] = {"quo": {"w": "see", "g": 1, "e": 0, "q": gens(line.s)["q_gen"]}}      # агент прежней версии
    assert line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed()), "шлюз без отпечатка — по номеру"


def test_qkeys_ok_of_another_polynomial_under_my_number_is_not_a_confirmation(line):
    _on(line)
    line.s.reserve_qkeys_reset()
    line.s.reserve_qkeys_ok_in({"l": "a", **gens(line.s), "qf": QF_A})
    assert not line.s.reserve_qkeys().get("acked"), "подтверждение чужого многочлена записано как моё"
    assert not line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed())
    line.s.reserve_qkeys_ok_in({"l": "a", **gens(line.s), "qf": _qf(line.s)})
    assert line.s.reserve_qkeys()["acked"]["qf"] == _qf(line.s)
    assert line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed())


def test_a_stale_ack_with_another_print_does_not_hold_after_a_new_polynomial_of_the_same_number(line):
    """Подтверждение записано с отпечатком, многочлен под тем же номером сменился (копия) —
    ключи не подтверждены, `qkeys` уходят заново."""
    _on(line)
    line.s.reserve_qkeys_ok_in({"l": "a", **gens(line.s), "qf": _qf(line.s)})
    q = line.s.reserve_qkeys()
    line.s.rsv_update("rsv_qkeys", poly=[("%02x" % (i + 7)) * rsvkeys.M_LEN for i in range(len(q["poly"]))])
    assert not line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed()), "чужой многочлен сочтён подтверждённым"
    line.advance(120)
    body = line.s.reserve_qkeys_body()
    assert body is not None and body["qf"] == _qf(line.s), body


def test_qkeys_ok_without_the_print_confirms_by_numbers(line):
    _on(line)
    line.s.reserve_qkeys_ok_in({"l": "a", **gens(line.s)})
    assert line.s.reserve_quorum_keys_ok(line.s.reserve_quorum_proposed())


def test_every_polynomial_of_the_current_m_is_remembered_and_a_new_m_starts_over(line):
    _on(line)
    first = _qf(line.s)
    line.s._quorum_new_poly(3, "смена порога")
    q = line.s.reserve_qkeys()
    assert q["m_qfs"] == [first, _qf(line.s)] and q["m_qfs_full"], q.get("m_qfs")
    line.s.reserve_new_generation("тест", code=False)
    q = line.s.reserve_qkeys()
    assert q["m_qfs"] == [_qf(line.s)] and first not in q["m_qfs"], "отпечатки прежнего M в списке нового"


# ── сигнал шлюза об отданной точке ─────────────────────────────────────────

def _pg(svc, **pg) -> None:
    svc.reserve_quorum_snapshot_in(1, {"w": "see", "g": 1, "e": 0, "pg": {"at": "2026-11-02T14:05:00", **pg}})


def test_a_given_point_of_a_polynomial_of_this_m_marks_the_key(mv):
    svc = mv.svc
    svc._quorum_new_poly(2, "подготовка")
    q0, first = int(svc.reserve_qkeys()["q_gen"]), svc._quorum_qf()
    svc._quorum_new_poly(3, "смена порога")
    _pg(svc, q=q0, qf=first)
    assert svc.reserve_compromised(), "точка многочлена текущего M (прежнего порога) не отмечена"


def test_a_given_point_with_a_foreign_print_under_a_current_number_is_not_this_key(mv):
    """Номер не ниже первого многочлена текущего `M`, а отпечаток не из его многочленов —
    точка многочлена до восстановления из копии: этот ключ ею не собрать."""
    svc = mv.svc
    svc._quorum_new_poly(2, "подготовка")
    q0 = int(svc.reserve_qkeys()["q_gen"])
    _pg(svc, q=q0, qf=QF_A)
    assert not svc.reserve_compromised(), "точка чужого многочлена того же номера пометила ключ"
    _pg(svc, q=q0)                                            # агент прежней версии — по номеру, как раньше
    assert svc.reserve_compromised()


def test_without_a_full_list_the_number_decides(mv):
    """Основа обновилась посреди поколения `M`: список многочленов неполный — по номеру
    (лишняя смена поколения безопаснее пропущенной)."""
    svc = mv.svc
    svc._quorum_new_poly(2, "подготовка")
    q0 = int(svc.reserve_qkeys()["q_gen"])
    svc.rsv_update("rsv_qkeys", m_qfs_full=False)
    _pg(svc, q=q0, qf=QF_A)
    assert svc.reserve_compromised()


# ── резерв ──────────────────────────────────────────────────────────────────

def _qkeys(qf: str = "") -> dict:
    body = {"l": LINE, "m_gen": M_GEN, "q_gen": Q_GEN, "thr": 2, "x": rsvkeys.RESERVE_X, "y": RSV_Y, "qbox": QBOX,
            "c_gen": 1, "cy": RSV_Y, "cbox": QBOX}
    return {**body, "qf": qf} if qf else body


def test_the_reserve_keeps_and_echoes_the_print(box):
    acts = box.s.reserve_qkeys_in(_qkeys(QF_A))
    assert sent(acts, "qkeys_ok")[0]["qf"] == QF_A and box.s.reserve_quorum_peer_keys()["qf"] == QF_A
    acts = box.s.reserve_qkeys_in(_qkeys())
    assert "qf" not in sent(acts, "qkeys_ok")[0], "отпечаток выдуман за основу прежней версии"
    assert box.s.reserve_qkeys_in({**_qkeys("zz"), "qf": "zz"}) and box.s.reserve_quorum_peer_keys()["qf"] == ""


def _to_points(cand) -> list[tuple]:
    cand.own("uns")
    cand.view(1, "uns")
    cand.view(2, "uns")
    acts = cand.tick() + cand.run(100) + cand.run(60)
    assert cand.stage() == "points", f"подготовка: исполнение не дошло до точек: {cand.stage()}"
    return acts


@pytest.fixture()
def cand_qf(box):
    """Кандидат о линии `b`, ящики приняты с отпечатком QF_A."""
    from tests.integration._reserve_vote_common import Cand
    c = Cand(box)
    box.s.reserve_qkeys_in(_qkeys(QF_A))
    return c


def test_the_need_carries_the_print_and_a_point_of_another_polynomial_is_not_taken(cand_qf):
    acts = _to_points(cand_qf)
    needs = gw_sent(acts, "qneed")
    assert needs and all(b["qf"] == QF_A for _slot, b in needs), needs
    out = cand_qf.s.reserve_guest_in(1, "qshare", {"l": LINE, "q_gen": Q_GEN, "x": rsvkeys.gateway_x(1), "y": GW1_Y,
                                                   "qf": QF_B})
    assert out == [] and "2" not in (cand_qf.s.__dict__["_quo_exec"].get("points") or {}), \
        "точка прежнего многочлена того же номера принята"
    cand_qf.s.reserve_guest_in(1, "qshare", {"l": LINE, "q_gen": Q_GEN, "x": rsvkeys.gateway_x(1), "y": GW1_Y,
                                             "qf": QF_A})
    assert "2" in cand_qf.s.__dict__["_quo_exec"]["points"]


def test_a_point_of_an_older_gateway_without_the_print_is_taken_by_the_number(cand_qf):
    _to_points(cand_qf)
    cand_qf.share(1)
    assert "2" in cand_qf.s.__dict__["_quo_exec"]["points"], "точка шлюза прежней версии отвергнута"


def test_without_a_print_the_need_has_none(cand):
    needs = gw_sent(_to_points(cand), "qneed")
    assert needs and all("qf" not in b for _slot, b in needs), needs
