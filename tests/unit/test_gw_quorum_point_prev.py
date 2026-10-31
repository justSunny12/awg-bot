"""Шлюз держит точку прежнего поколения многочлена (domain/gateway/reserve/quorum.py
`quorum_apply`, `_point_prev`, `_point_of_gen`, `_quorum_need_in`).

Новый многочлен шлюз получает документом сразу, второй сервер свои точки и ящики (`qkeys`)
— только по живому линку; кандидат в этом окне просит точку прежнего поколения (того, чей
ящик у него). Шлюз отдаёт её из `point_prev`: одно прежнее поколение, до следующей смены;
голосование выключено (точки нет) — и прежней нет. Правило «отдаю только при своём «не
вижу»» — то же.

Цена ошибки: без прежней точки кандидат получает отказ «поколение не то» и уходит на путь
по коду через минуту, хотя голосов для переезда хватает; точка поколения, которого никто
не просил, — лишняя точка у кандидата.
"""
from __future__ import annotations

import pytest

from tests.unit.test_gw_quorum_decision import POINT_Y, Q_GEN, ag, clock

pytestmark = pytest.mark.unit
__all__ = ["ag", "clock"]

NEW_Y = "ef" * 16


def _apply(ag, point: dict) -> None:
    sec = ag.section()
    doc = {k: sec[k] for k in ("l", "g", "members", "t", "port", "probes", "e")}
    ag.svc.quorum_apply({**doc, "point": point})


def _share(q_gen: int, y: str) -> list:
    return [("guest", "qshare", {"l": "a", "q_gen": q_gen, "x": 2, "y": y})]


def test_after_a_new_polynomial_the_old_generation_is_still_given(ag):
    _apply(ag, {"x": 2, "y": NEW_Y, "q_gen": Q_GEN + 1})
    ag.uns()
    assert ag.word("qneed", q_gen=Q_GEN) == _share(Q_GEN, POINT_Y), "кандидат с ящиком прежнего поколения получил отказ"
    assert ag.word("qneed", q_gen=Q_GEN + 1) == _share(Q_GEN + 1, NEW_Y)
    assert ag.word("qneed", q_gen=Q_GEN - 1) == [], "отдана точка поколения, которого у шлюза нет"


def test_only_one_previous_generation_is_kept(ag):
    _apply(ag, {"x": 2, "y": NEW_Y, "q_gen": Q_GEN + 1})
    _apply(ag, {"x": 2, "y": "12" * 16, "q_gen": Q_GEN + 2})
    ag.uns()
    assert ag.word("qneed", q_gen=Q_GEN) == [], "держится больше одного прежнего поколения"
    assert ag.word("qneed", q_gen=Q_GEN + 1) == _share(Q_GEN + 1, NEW_Y)


def test_the_same_document_again_keeps_the_previous_point(ag):
    _apply(ag, {"x": 2, "y": NEW_Y, "q_gen": Q_GEN + 1})
    _apply(ag, {"x": 2, "y": NEW_Y, "q_gen": Q_GEN + 1})
    assert ag.section()["point_prev"]["q_gen"] == Q_GEN, "повтор документа стёр прежнюю точку"


def test_no_point_means_no_previous_point_either(ag):
    _apply(ag, {"x": 2, "y": NEW_Y, "q_gen": Q_GEN + 1})
    _apply(ag, {})
    assert "point_prev" not in ag.section()
    ag.uns()
    assert ag.word("qneed", q_gen=Q_GEN) == [] and ag.word("qneed", q_gen=Q_GEN + 1) == []


def test_the_previous_point_is_not_given_while_seeing(ag):
    _apply(ag, {"x": 2, "y": NEW_Y, "q_gen": Q_GEN + 1})
    ag.see()
    assert ag.word("qneed", q_gen=Q_GEN) == [], "прежняя точка отдана при видимой основе"
