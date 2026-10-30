"""Возврат домой (q2 → лидер дома) не ставит ограждение «старт»: эпоху отдал гость явно,
кандидат забрать не мог; ограждение до первого большинства «да» отвергало бы правки
админа и слово wiped второй стороны сразу после возврата (стенд, s06 «з»). Старт
процесса и включение линии — по-прежнему с ограждением."""
from __future__ import annotations

import pytest

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_quorum_cov_leader import _Q, _lead

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


@pytest.fixture()
def q(mv, monkeypatch):
    w = _Q(mv.svc, monkeypatch)
    return w


def _away(q, monkeypatch) -> None:
    monkeypatch.setattr(q.s, "reserve_quorum_leader_host", lambda: "b")
    monkeypatch.setattr(q.s, "reserve_out", lambda: "away")
    q.tick()
    assert q.s._qleader is not None and q.s._qleader.q2, "подготовка: ответчик дома в отъезде не считает Q2"


def _home(q, monkeypatch) -> None:
    monkeypatch.setattr(q.s, "reserve_quorum_leader_host", lambda: "a")
    monkeypatch.setattr(q.s, "reserve_out", lambda: "home")


def test_the_start_of_the_process_still_fences(q):
    q.tick()
    assert q.s.rsv_get("rsv_qlog").get("fenced", {}).get("start") is True, "старт процесса без ограждения"
    assert q.meta()[1] == "follower"


def test_coming_home_after_the_move_does_not_fence(q, monkeypatch):
    _lead(q)
    _away(q, monkeypatch)
    _home(q, monkeypatch)
    before = q.meta()
    acts = q.tick()
    assert not q.s.__dict__.get("_quo_start_pending"), "возврат домой поставил ограждение «старт»"
    assert q.meta() == before, f"возврат домой тронул журнал: {q.meta()} против {before}"
    assert q.meta()[:2] == ("self", "holder"), "после возврата основа не держатель"
    assert not q.s._qleader.fenced and not q.s._qleader.starting
    assert acts == [], acts
    q.run(30, yes=("b", "a1"))
    assert q.meta()[:2] == ("self", "holder"), "основа потеряла право записи после возврата при большинстве «да»"
    q.run(60)                                                  # голосов нет дольше T_down — обычное ограждение, не «старт»
    assert q.meta()[1] == "follower" and q.s.rsv_get("rsv_qlog").get("fenced", {}).get("start") is not True
