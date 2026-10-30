"""Матрица резерва под кворумом второй стороны (domain/services/reserve/vote.py:
`_quorum_rules`, `_liveness_views`; концепт «кворум» §3.7.9, §3.8).

У линии основы с включённым кворумом (и `act`) переезд и аварийный код — у
правила кандидата; матрице остаётся только Н3: набор — шлюзы основы с живым
гостевым каналом (взгляд — их `qview`), ни одного в наборе и резерв видит
основу — Н3 (`view` основе и демпфер `ask{nogw}`, как раньше); любой другой
расклад — случай «6», без `need`, `_auto_move` и окна кода.

Мир — резерв «Alpha» в паре с «Beta» (tests/integration/_reserve_vote_common.py),
шлюзы «Beta» — слоты 1–3; кворум линии `b` — поле `quo` её `st`.

Цена ошибки: матрица рядом с кворумом просит доли или открывает окно кода —
второй путь переезда без большинства, два бота у одних пользователей; Н3 не
наступает, когда шлюзов основы нет на связи, — РФ-часть её пользователей
не работает, хотя резерв мог бы её дать; Н3 при живом канале шлюза — переезд
«шлюзов нет» при живых шлюзах.
"""
from __future__ import annotations

import pytest

from awgbot.core import settings
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import box, gw_sent, sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]

LINE = "b"
MEMBERS = ["b", "a", "b1", "b2", "b3"]
DAMPER = 15 * 60


def _quorum(box, on: bool = True) -> None:
    settings.set_value("app.reserve.quorum.act", True)
    box.s.rsv_update("rsv_peer_state", quo={"l": LINE, "g": 1, "members": list(MEMBERS), "active": on, "why": []})


def _qview(box, slot: int, w: str) -> list[tuple]:
    return box.s.reserve_guest_in(slot, "qview", {"l": LINE, "g": 1, "w": w, "e": 7, "since": "2026-10-30T14:05:00+03:00"})


def _run(box, seconds: float, step: float = 5) -> list[tuple]:
    acts: list[tuple] = []
    t = 0.0
    while t < seconds:
        box.advance(step)
        t += step
        if int(t) % 30 == 0:
            box.keepalive()
        acts += box.tick()
    return acts


@pytest.fixture()
def q(box, monkeypatch):
    """Кворум «Beta» включён; у резерва свой шлюз — Н3 есть чем дать."""
    monkeypatch.setattr(box.s.db, "gateways", lambda: [object()])
    _quorum(box)
    return box


def test_no_gateway_channel_and_the_primary_seen_is_h3_with_the_view_to_the_primary(q):
    q.primary(True)
    acts = q.tick()
    assert q.case == "H3", f"под кворумом шлюзов основы нет на связи, а случай {q.case!r}"
    views = [b for b in sent(acts, "view") if b.get("case") == "H3"]
    assert len(views) == 1, f"основе не ушёл view{{H3}}: {acts}"


def test_h3_under_the_quorum_still_asks_nogw_after_the_damper(q):
    q.primary(True)
    acts = q.tick() + _run(q, DAMPER + 60)
    asks = sent(acts, "ask")
    assert asks and asks[0]["case"] == "nogw", f"Н3 под кворумом без ask{{nogw}}: {asks}"
    assert not gw_sent(acts, "need"), "Н3 просит доли"


def test_a_dead_gateway_channel_is_out_of_the_set(q):
    """Обрыв сессии — шлюз вне набора: у Н3 под кворумом критерий — живость канала."""
    q.primary(True)
    q.hello(1)
    _qview(q, 1, "uns")
    q.tick()
    assert q.case == "6", f"шлюз на связи, а случай {q.case!r}"
    q.gone(1)
    q.tick()
    assert q.case == "H3", f"канал шлюза мёртв — он всё ещё в наборе: случай {q.case!r}"


@pytest.mark.parametrize("sees,w", [(True, "uns"), (True, "see"), (False, "uns"), (False, "see"), (False, "")])
def test_any_other_layout_is_case_6_without_need_or_code(q, sees, w):
    q.primary(sees)
    acts = q.hello(1, sees=(w != "uns"))
    if w:
        acts += _qview(q, 1, w)
    acts += q.tick() + _run(q, 600)
    assert q.case == "6", f"под кворумом матрица решила {q.case!r} (резерв видит: {sees}, шлюз: {w or 'молчит'})"
    assert not gw_sent(acts, "need"), "матрица под кворумом просит долю"
    assert not q.s.reserve_code_window()["open"], "матрица под кворумом открыла окно кода"
    assert not q.launched


def test_without_any_gateway_and_the_primary_down_the_matrix_opens_no_code_window(q):
    """Без кворума это Н4 — окно кода; под кворумом окно ведёт кандидат."""
    q.primary(False)
    acts = q.tick() + _run(q, 120)
    assert q.case == "6" and not q.s.reserve_code_window()["open"], f"случай {q.case!r}, окно кода открыто матрицей"
    assert not [a for a in acts if a and a[0] == "notify"], "матрица под кворумом шлёт уведомления окна кода"


def test_without_the_quorum_the_matrix_is_as_before(box):
    """Контроль: кворум второй стороны не включён — прежняя матрица (Н4 и окно кода,
    случай 1 и просьба доли)."""
    _quorum(box, on=False)
    box.primary(False)
    box.tick()
    assert box.case == "H4" and box.s.reserve_code_window()["open"], f"без кворума Н4 не открыл окно: {box.case!r}"
    acts = box.hello(1, sees=False) + box.tick()
    assert box.case == "1" and gw_sent(acts, "need"), f"без кворума случай 1 не просит долю: {box.case!r}"


def test_with_act_off_the_matrix_is_as_before(box):
    _quorum(box)
    settings.set_value("app.reserve.quorum.act", False)
    box.primary(False)
    acts = box.hello(1, sees=False) + box.tick()
    assert box.case == "1" and gw_sent(acts, "need"), "act=false: матрица выключена кворумом"
