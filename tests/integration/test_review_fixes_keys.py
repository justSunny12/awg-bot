"""Правки по ревью ключей кворума (domain/services/reserve/quorum/keys.py, members.py,
session.py, opening.py):

- новая сессия пары снимает подтверждение `qkeys` второй стороны — точки и ящики уходят
  заново (резерв мог быть восстановлен из копии без них);
- у резерва ящики прежнего поколения `M` живут до прихода пакета нового: окно между
  `qkeys` и пакетом не оставляет «данных для переезда нет»;
- смена поколения ключей (новое `M`, порог, «🔄 Сменить код») не выключает линию,
  пока ключи были подтверждены хоть раз (`rsv_qkeys.confirmed`); первое включение и
  включение после выключения голосования ждут подтверждения.

Миры: линия «Alpha» с шлюзами (`line`/`on`, tests/integration/_reserve_quorum.py) и
резерв с кандидатом о «Beta» (`box`, tests/integration/_reserve_vote_common.py).

Цена ошибки: резерв, восстановленный из копии без точек и ящиков, так и не получает их —
переезд по кворуму и по коду невозможен, а основа считает ключи на месте; без ящиков
прежнего поколения до нового пакета резерв на минуты без возможности переезда; линия,
выключенная сменой поколения, — ответчик молчит, шлюзы берут обязательства «нет» и
живой лидер лишается кворума; линия, включённая без подтверждения после выключения, —
голоса есть, а ключа под них у второго сервера нет.
"""
from __future__ import annotations

import pytest

from awgbot.util import rsvkeys

from tests.integration import _reserve_common as rc
from tests.integration._reserve_quorum import line
from tests.integration._reserve_vote_common import LINE, M_GEN, Q_GEN, QBOX, RSV_Y, box

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["line", "box"]


@pytest.fixture()
def on(line):
    """Кворум «Alpha» включён: шлюз 1 с quo1, «Beta» и шлюз приняли ключи."""
    line.add_gw(1)
    line.enable()
    line.s.reserve_code_notify_clear()
    return line


def _keys_on(s) -> bool:
    return "keys" not in s.reserve_quorum_off_reasons()


# ── новая сессия пары (keys-1) ──────────────────────────────────────────────

def test_a_new_pair_session_resends_even_confirmed_qkeys(on):
    """Ключи подтверждены, повторов нет; новая сессия пары (резерв мог прийти из копии) —
    `qkeys` уходят заново сразу, а не никогда."""
    on.advance(120)
    assert on.s.reserve_qkeys_body() is None, "подготовка: подтверждённые ключи шлются снова"
    on.s.reserve_session_opened({})
    assert on.s.reserve_qkeys().get("acked") == {}, "новая сессия не сняла подтверждение второй стороны"
    body = on.s.reserve_qkeys_body()
    assert body is not None and body["m_gen"] == on.gens()["m_gen"], "после новой сессии qkeys не ушли второй стороне"


def test_without_a_new_session_confirmed_qkeys_are_not_repeated(on):
    for _ in range(3):
        on.advance(120)
        assert on.s.reserve_qkeys_body() is None, "подтверждённые ключи шлются без новой сессии"


def test_a_new_session_does_not_turn_the_line_off(on):
    """Сброс подтверждения второй стороны — повод послать ключи, не выключить линию:
    подтверждены они были, отметка `confirmed` держит линию."""
    on.s.reserve_session_opened({})
    assert on.s.reserve_quorum_active(), f"новая сессия пары выключила линию: {on.s.reserve_quorum_off_reasons()}"


def test_the_ack_after_a_new_session_closes_the_resend(on):
    on.s.reserve_session_opened({})
    assert on.s.reserve_qkeys_body() is not None
    on.ack_keys()
    on.advance(120)
    assert on.s.reserve_qkeys_body() is None, "после подтверждения в новой сессии qkeys шлются снова"


# ── ящики прежнего поколения у резерва (keys-2) ─────────────────────────────

def _qkeys(m_gen: int) -> dict:
    return {"l": LINE, "m_gen": m_gen, "q_gen": Q_GEN, "thr": 2, "x": rsvkeys.RESERVE_X, "y": RSV_Y, "qbox": QBOX,
            "c_gen": 1, "cy": RSV_Y, "cbox": QBOX}


@pytest.fixture()
def held(box):
    """Резерв держит пакет поколения M_GEN и точки с ящиками того же поколения."""
    assert box.s.reserve_qkeys_in(_qkeys(M_GEN)), "подготовка: точки не приняты"
    assert box.s.reserve_open_block() == "", f"подготовка: открытие закрыто: {box.s.reserve_open_block()}"
    return box


def test_new_qkeys_before_the_new_package_keep_the_old_boxes_in_play(held):
    held.s.reserve_qkeys_in(_qkeys(M_GEN + 1))
    prev = held.s.reserve_qkeys().get("peer_prev") or {}
    assert int(prev.get("m_gen") or 0) == M_GEN, f"прежнее поколение ящиков не сохранено: {prev}"
    assert held.s.reserve_quorum_peer_keys()["m_gen"] == M_GEN, "резерв отдаёт ящики нового M под пакет прежнего"
    assert held.s.reserve_open_block(need_box=True) == "", \
        f"между qkeys и пакетом нового M — «{held.s.reserve_open_block()}»"


def test_the_new_package_switches_to_the_new_boxes(held):
    held.s.reserve_qkeys_in(_qkeys(M_GEN + 1))
    held.s.rsv_update("rsv_pkg_in", gen=4, m_gen=M_GEN + 1)
    assert held.s.reserve_quorum_peer_keys()["m_gen"] == M_GEN + 1, "пакет нового M пришёл, а ящики прежние"
    assert held.s.reserve_open_block(need_box=True) == ""


def test_the_same_generation_again_keeps_no_previous(held):
    held.s.reserve_qkeys_in(_qkeys(M_GEN))
    assert not held.s.reserve_qkeys().get("peer_prev"), "повтор того же поколения записан как прежнее"


def test_a_package_of_neither_generation_is_no_data_for_the_move(held):
    held.s.reserve_qkeys_in(_qkeys(M_GEN + 1))
    held.s.rsv_update("rsv_pkg_in", m_gen=M_GEN + 5)
    assert held.s.reserve_open_block(need_box=True) == "данных для переезда нет", \
        "ящики чужого поколения сочтены годными для пакета"


# ── подтверждение хоть раз (quorum-2) ───────────────────────────────────────

def test_the_first_ack_marks_the_keys_confirmed(on):
    assert on.s.reserve_qkeys().get("confirmed") is True, "после первого подтверждения отметки confirmed нет"


def test_the_first_enabling_waits_for_the_ack(line):
    line.add_gw(1)
    line.propose()
    line.ack_members()
    line.sync_keys()
    assert "keys" in line.s.reserve_quorum_off_reasons(), "первое включение — линия до подтверждения ключей"
    assert not line.s.reserve_qkeys().get("confirmed")
    line.ack_keys()
    line.gateways_applied()
    assert line.s.reserve_quorum_active() and line.s.reserve_qkeys().get("confirmed") is True


def test_a_new_m_keeps_the_line_on_until_the_new_keys_are_confirmed(on):
    on.s._reserve_after_gateway_change("после снятия шлюза")
    assert not on.s.reserve_quorum_keys_ok(on.s.reserve_quorum_mine()), "подготовка: новые ключи уже подтверждены"
    assert _keys_on(on.s), "новое M выключило линию до подтверждения новых ключей"
    assert on.s.reserve_quorum_active(), f"новое M — лидер без линии: {on.s.reserve_quorum_off_reasons()}"


def test_the_code_change_button_keeps_the_line_on(on):
    on.s.reserve_new_generation("по кнопке «🔄 Сменить код»")
    assert not on.s.reserve_quorum_keys_ok(on.s.reserve_quorum_mine()), "подготовка: новые ключи уже подтверждены"
    assert _keys_on(on.s) and on.s.reserve_quorum_active(), "«🔄 Сменить код» выключило линию"


def test_a_new_threshold_keeps_the_keys_from_turning_the_line_off(on):
    q0 = on.gens()["q_gen"]
    on.add_gw(2)
    on.add_gw(3)
    on.propose()
    on.ack_members()
    on.sync_keys()
    assert on.gens()["q_gen"] == q0 + 1, "подготовка: порог не сменился"
    assert _keys_on(on.s), f"смена порога выключила линию по ключам: {on.s.reserve_quorum_off_reasons()}"


def test_switching_the_vote_off_drops_the_mark_and_the_next_enabling_waits(on):
    on.s.reserve_quorum_keys_sync(on.s.reserve_quorum_proposed(), False)
    assert not on.s.reserve_qkeys().get("confirmed"), "выключение голосования оставило отметку confirmed"
    on.sync_keys()
    assert on.s.reserve_quorum_poly_on(), "подготовка: многочлен не вернулся"
    assert "keys" in on.s.reserve_quorum_off_reasons(), "повторное включение — линия до подтверждения новых ключей"
    on.ack_keys()
    on.gateways_applied()
    assert _keys_on(on.s) and on.s.reserve_qkeys().get("confirmed") is True


def test_the_last_gateway_going_drops_the_mark(on):
    on.gw_quo(1, False)
    on.propose()
    on.ack_members()
    on.sync_keys()
    assert not on.s.reserve_quorum_poly_on(), "подготовка: многочлен остался"
    assert not on.s.reserve_qkeys().get("confirmed"), "уход последнего шлюза оставил отметку confirmed"
    on.gw_quo(1, True)                                    # шлюз вернулся: новое включение
    on.propose()
    on.ack_members()
    on.sync_keys()
    assert on.s.reserve_quorum_poly_on(), "подготовка: многочлен не вернулся"
    assert "keys" in on.s.reserve_quorum_off_reasons(), "включение после ухода всех шлюзов — без подтверждения ключей"
