"""Правки по ревью — взгляды шлюзов основы у резерва (domain/services/reserve/gwguest.py,
quorum/inbound.py): новая сессия гостевого канала делает прежний взгляд шлюза неизвестным
и спрашивает его заново (`qask`); такт канала и экран обходят память взглядов копией.

Сценарий — `_Box` и `Cand` из _reserve_vote_common (линия `b` второй стороны, кандидат —
этот сервер, шлюзы b1, b2 на гостевом канале).

Цена ошибки: между сессиями шлюз снова видит основу, а слово об этом не дошло (сессии не
было); прежнее «нет» по-прежнему в счёте на живом канале — ложное большинство, `qtake` и
просьба `ask{q}` при живой основе; поток слов, вставляющий взгляд во время обхода памяти
тактом, роняет такт (RuntimeError) — счёт кандидата пропускает шаг.
"""
from __future__ import annotations

import pytest

from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import LINE, Cand, box, gw_sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]


def test_a_new_session_forgets_the_gateway_view_and_asks_again(box):
    c = Cand(box)
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "uns")
    c.tick()
    assert box.s.reserve_quorum_candidate_view()["no"] == 3, "подготовка: «нет» обоих шлюзов не в счёте"
    acts = box.hello(1)                                       # обрыв и новая сессия без reserve_guest_closed
    assert "b1" not in box.s.reserve_quorum_gw_views(LINE), "прежний взгляд шлюза пережил новую сессию"
    assert gw_sent(acts, "qask") == [(1, {"l": LINE, "g": 1})], f"новой сессии не задан вопрос о взгляде: {acts}"
    c.tick()
    assert box.s.reserve_quorum_candidate_view()["no"] == 2, "прежнее «нет» шлюза осталось в счёте кандидата"
    c.view(1, "see")
    c.tick()
    assert box.s.reserve_quorum_candidate_view()["no"] == 2


def test_the_forgotten_vote_keeps_the_pledge_for_qrel(box):
    """Голос неизвестен, но «нет» шлюз сказал — обязательство снимает только qrel/qtake."""
    c = Cand(box)
    c.own("uns")
    c.view(1, "uns")
    c.tick()
    box.hello(1)
    vt = box.s._qcand.votes["b1"]
    assert vt.w == "new" and vt.said_no, "обязательство шлюза потеряно — qrel ему не уйдёт"


def test_a_first_session_has_no_question(box):
    Cand(box, hello=False)
    acts = box.hello(1)
    assert not gw_sent(acts, "qask"), "вопрос о взгляде без прежнего взгляда — лишнее слово"


def test_a_gateway_without_quorum_gets_no_question(box):
    c = Cand(box)
    c.view(1, "uns")
    acts = box.hello(1, caps=["rsv1"])
    assert not gw_sent(acts, "qask") and "b1" not in box.s.reserve_quorum_gw_views(LINE)


# ── эпоха гостя — шлюзам и своим пробам, пока её пользователи у меня ─────────

def test_while_hosting_the_gateways_and_my_probes_get_the_epoch_of_the_guest(box, monkeypatch):
    c = Cand(box, hello=False)
    box.s.reserve_set_in("serving")
    box.s.reserve_guest_jrn_seen({"e": 9, "fork": 0, "last": 3})   # гость взял под 8 + 1
    assert box.s._state_body()["e"] == 9, "state шлюзам несёт эпоху основы, а не гостя"
    acts = box.hello(1)
    q = gw_sent(acts, "qstate")
    assert q and q[0][1]["e"] == 9 and q[0][1]["holder"] == "self", f"qstate новой сессии без эпохи гостя: {q}"
    monkeypatch.setattr(box.s, "reserve_quorum_probe_ifaces",
                        lambda: {f"awgq{LINE}h": {"l": LINE, "target": "h", "resp": "169.254.88.1", "port": 8789}})
    box.s.reserve_quorum_voter_tick(box.clock.t)
    assert c.v.epoch == 9, "мои пробы к её дому — с прежней эпохой: вернувшаяся основа сочтёт моё «да»"


def test_without_hosting_the_guest_epoch_is_not_taken(box):
    Cand(box, hello=False)
    box.s.reserve_guest_jrn_seen({"e": 9})
    assert box.s._state_body().get("e") == 7, "эпоха гостя без приёма пользователей — не факт"


# ── обход памяти взглядов и сессий — копией ─────────────────────────────────

class _Inserting(dict):
    """Запись, при чтении которой «поток слов» вставляет новую — как qview/hello из другого потока."""

    def __init__(self, outer: dict, key, val, **kw):
        super().__init__(**kw)
        self._outer, self._key, self._val = outer, key, val

    def get(self, k, default=None):
        if self._key not in self._outer:
            self._outer[self._key] = self._val
        return super().get(k, default)


def test_the_views_of_the_line_survive_a_word_inserted_meanwhile(box):
    Cand(box, hello=False)
    views = box.s._qgwviews
    views["b1"] = _Inserting(views, "b2", {"w": "see", "l": LINE}, w="uns", l=LINE)
    got = box.s.reserve_quorum_gw_views(LINE)                 # прежде: RuntimeError — такт кандидата терялся
    assert "b1" in got


def test_the_state_word_survives_a_session_opened_meanwhile(box):
    box.hello(1)
    gwv = box.s._gwv
    gwv[1] = _Inserting(gwv, 3, {"online": False}, **dict(gwv[1]))
    acts = box.s.reserve_gw_state_actions(force=True)          # прежде: RuntimeError — state шлюзам не уходил
    assert gw_sent(acts, "state") and gw_sent(acts, "state")[0][0] == 1
    assert box.s.reserve_gw_links_view()
