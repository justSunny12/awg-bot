"""Держатель точки ушёл из состава — новое поколение `M` (domain/services/reserve/quorum/keys.py
`reserve_quorum_keys_sync`, `_quorum_holders_gone`, `_quorum_holders_note`, `rsv_qkeys.holders`).

Шлюз, получивший точку многочлена при текущем поколении `M`, ушёл из состава без снятия
слота (агент откатился ниже `quo1`, hello без него): его точка и точка второго сервера при
прежнем пороге открывали бы текущий ящик кворума (или прежний — с тем же `M`). Поэтому —
новое поколение `M` и `K`, многочлен заново, код прежний. Снятие слота уже меняет поколение
(keys `_reserve_after_gateway_change`) — второй раз на следующем такте не нужно, в том числе
при выключении голосования после снятия последнего шлюза. Рост состава — только новый
многочлен.

Мир: линия «Alpha» с шлюзами (`line`, tests/integration/_reserve_quorum.py).

Цена ошибки: откатившийся (или отданный чужому) шлюз со своей точкой и скомпрометированный
резерв открывают пакет мимо большинства; лишняя смена поколения на снятии — второй пакет и
окно, в котором у резерва ящики не того поколения.
"""
from __future__ import annotations

import pytest

from awgbot.util import rsvkeys

from tests.integration import _reserve_common as rc
from tests.integration._reserve_quorum import line

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["line"]


@pytest.fixture()
def two(line):
    """Кворум «Alpha» включён с двумя шлюзами (v = 4)."""
    line.add_gw(1)
    line.add_gw(2)
    line.enable()
    line.s.reserve_code_notify_clear()
    return line


def _reserve_point_and_qbox(s) -> tuple[tuple[int, bytes], bytes]:
    """Точка второго сервера и ящик кворума текущего многочлена — то, что у него лежит."""
    q = s.reserve_qkeys()
    poly = s._quorum_poly()
    qg = rsvkeys.QuorumGen(int(q["q_gen"]), poly)
    return qg.reserve_point(), qg.qbox(s.reserve_m())


def _gw_point(s, slot: int) -> tuple[int, bytes]:
    return s._quorum_poly().point(rsvkeys.gateway_x(slot))


def _shrink(ln, slot: int) -> list:
    ln.gw_quo(slot, False)
    ln.propose()
    ln.ack_members()
    return ln.sync_keys()


def test_a_holder_rolled_back_below_quo1_makes_a_new_generation_and_keeps_the_code(two):
    s = two.s
    g0, code0 = two.gens(), s.reserve_code()
    m0 = s.reserve_m()
    res_pt, old_qbox = _reserve_point_and_qbox(s)
    old_pts = [res_pt, _gw_point(s, 1), _gw_point(s, 2)]       # у резерва его точка и ящик, у шлюзов — свои
    assert rsvkeys.recover_m_quorum(old_pts, old_qbox) == m0, "предпосылка: прежние точки открывают ящик"
    _shrink(two, 2)
    g1 = two.gens()
    assert g1["m_gen"] == g0["m_gen"] + 1 and s.reserve_m() != m0, f"держатель ушёл с точкой, а M прежний: {g1}"
    assert g1["c_gen"] == g0["c_gen"] and s.reserve_code() == code0 and not s.reserve_code_notify_pending(), \
        "уход держателя точки сменил код"
    # точки ушедшего и резерва (и прежний ящик) дают только прежний M — пакет уже под новым
    assert rsvkeys.recover_m_quorum(old_pts, old_qbox) != s.reserve_m()
    _res_now, qbox_now = _reserve_point_and_qbox(s)
    try:
        got = rsvkeys.recover_m_quorum([res_pt, _gw_point_of(old_pts, 2)], qbox_now)
    except ValueError:
        got = b""
    assert got != s.reserve_m(), "точка ушедшего и точка резерва открывают текущий ящик"


def _gw_point_of(pts, slot: int):
    return next(p for p in pts if p[0] == rsvkeys.gateway_x(slot))


def test_a_holder_gone_is_rotated_once_not_every_tick(two):
    _shrink(two, 2)
    g1 = two.gens()["m_gen"]
    for _ in range(3):
        two.sync_keys()
    assert two.gens()["m_gen"] == g1, "смена поколения повторяется каждый такт"


def test_a_new_gateway_joining_keeps_m(two):
    m_gen = two.gens()["m_gen"]
    two.add_gw(3)
    two.propose()
    two.ack_members()
    two.sync_keys()
    assert two.gens()["m_gen"] == m_gen, "рост состава сменил M"


def test_a_removed_slot_changes_the_generation_once(two):
    """Снятие слота меняет поколение само; следующий такт с новым составом — не второй раз."""
    m_gen = two.gens()["m_gen"]
    two.s._reserve_after_gateway_change("после снятия шлюза")
    two.remove_gw(2)
    two.propose()
    two.ack_members()
    two.sync_keys()
    assert two.gens()["m_gen"] == m_gen + 1, f"снятие слота: поколение {two.gens()['m_gen']}, ждали {m_gen + 1}"


def test_removing_the_last_gateway_changes_the_generation_once(line):
    line.add_gw(1)
    line.enable()
    m_gen = line.gens()["m_gen"]
    line.s._reserve_after_gateway_change("после снятия шлюза")
    line.remove_gw(1)
    line.propose()
    line.ack_members()
    line.sync_keys()
    assert not line.s.reserve_quorum_poly_on()
    assert line.gens()["m_gen"] == m_gen + 1, "снятие последнего шлюза и выключение голосования — две смены поколения"


def test_a_holder_leaving_while_the_vote_is_off_still_changes_m(two):
    """Голосование выключено (условия пропали), шлюзы в составе — M прежний; потом держатель
    ушёл — его точка прежнего многочлена с прежним ящиком у резерва всё ещё дала бы M."""
    s = two.s
    s.reserve_quorum_keys_sync(s.reserve_quorum_proposed(), False)
    m_gen = two.gens()["m_gen"]
    two.gw_quo(2, False)
    two.propose()
    two.ack_members()
    s.reserve_quorum_keys_sync(s.reserve_quorum_proposed(), False)
    assert two.gens()["m_gen"] == m_gen + 1, "держатель ушёл при выключенном голосовании — M прежний"
