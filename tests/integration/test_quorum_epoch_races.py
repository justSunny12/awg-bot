"""Роль эпохи (domain/services/reserve/journal/epoch.py `_epoch_sync_locked`) после
принятия чужой эпохи по кворуму (`jrn_adopt`, quorum/fence.py), когда линия кворума
между принятием и тактом перестала решать: выключена (ключи, состав, цифры, пересечения
подсетей) или осталась с одним голосующим (шлюзы ушли из состава, v = 1).

Правило лидера в этих случаях `unfence` не выдаёт, и страховка такта возвращала
эпоху себе сразу — в то время как кандидат второй стороны, сказавший «забираю», ещё
открывает зону и поднимет гостя под принятой эпохой: два писателя одной линии. Эпоха
возвращается ролью дома только по факту живой второй стороны, сказавшей после
принятия, что моих пользователей у неё нет (как и по правилу лидера, §3.4.2).

Мир — `_Q` из test_reserve_quorum_cov_leader (основа «Alpha» дома, линия `a`, состав a,
b, a1, a2); выключение линии — подмена предиката `reserve_quorum_active`.

Цена ошибки: гость на резерве и основа пишут решения под разными эпохами одной линии
— правки одной стороны теряются при встрече (хвост С9 поверх, события гостя затирают
мои), устройства раздваиваются.
"""
from __future__ import annotations

import pytest

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_quorum_epoch_machine import _peer_down, _peer_hosting_now, _peer_idle_now
from tests.integration.test_reserve_quorum_cov_leader import _Q, _fence

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


@pytest.fixture()
def q(mv, monkeypatch):
    return _Q(mv.svc, monkeypatch)


def _adopted(q, monkeypatch) -> int:
    """Лишённая кворума основа приняла e + 1 по пробе шлюза (кандидат сказал «забираю»)."""
    _fence(q)
    e = q.s.reserve_quorum_epoch()
    _peer_down(q, monkeypatch)
    q.probe("a1", e=e + 1)
    assert q.meta() == ("peer", "follower", e + 1), "подготовка: эпоха по пробе не принята"
    return e


@pytest.mark.parametrize("how", ["off", "single_voter"])
def test_the_line_switched_off_after_the_adoption_does_not_take_the_epoch_back_blindly(q, monkeypatch, how):
    e = _adopted(q, monkeypatch)
    if how == "off":
        q.active = False                                      # линия выключена (ключи, состав, цифры…)
    else:
        q.members(["a", "b"])                                 # шлюзы ушли из состава: голосующий один
    for _ in range(3):
        q.s.reserve_epoch_check()
        q.s.reserve_out_tick()
    assert q.meta() == ("peer", "follower", e + 1), \
        f"эпоха вернулась сюда без слова второй стороны — кандидат ещё может открывать: {q.meta()}"
    assert q.s.reserve_quorum_adopted(), "отметка принятия снята"


@pytest.mark.parametrize("how", ["off", "single_voter"])
def test_the_peers_idle_word_after_the_adoption_returns_the_epoch_without_the_leader_rule(q, monkeypatch, how):
    """Линия не решает, правило лидера молчит — факт живой второй стороны («моих не несу»,
    `st` после принятия) возвращает эпоху ролью дома; отметка принятия снимается."""
    e = _adopted(q, monkeypatch)
    if how == "off":
        q.active = False
    else:
        q.members(["a", "b"])
    q.s.reserve_epoch_check()
    assert q.meta()[0] == "peer"
    _peer_idle_now(q, monkeypatch)
    q.s.reserve_epoch_check()
    assert q.meta() == ("self", "holder", e + 2), f"факт второй стороны не вернул эпоху: {q.meta()}"
    assert not q.s.reserve_quorum_adopted(), "отметка принятия осталась после возврата эпохи"


def test_the_line_switched_off_and_the_peer_serving_goes_away_the_usual_way(q, monkeypatch):
    """Кандидат открыл: живой `st` второй стороны с `in = serving` после перехода домой — away
    обычным правилом (переезд мимо меня), эпоха у гостя, не +1 здесь."""
    e = _adopted(q, monkeypatch)
    q.active = False
    q.s.reserve_epoch_check()
    _peer_hosting_now(q, monkeypatch)
    q.s.reserve_out_tick()
    assert q.s.reserve_out() == "away", "резерв несёт моих после принятия — а основа дома"
    assert q.meta() == ("peer", "follower", e + 1), f"эпоха тронута при переезде мимо меня: {q.meta()}"


def test_the_stale_idle_word_from_before_the_adoption_is_not_the_fact(q, monkeypatch):
    """`st` второй стороны, полученный до принятия (idle — она ещё не решала), фактом не
    считается: эпоха остаётся у неё."""
    from awgbot.util import timeutil
    import time
    e = _adopted(q, monkeypatch)
    q.active = False
    monkeypatch.setattr(q.s, "reserve_peer_seen", lambda: "ok")
    q.s.rsv_update("rsv_peer_state", session=True, inn="idle", st_stale=False,
                   at=timeutil.to_iso(timeutil.shift(timeutil.now(), -60)), at_ts=time.time() - 60)
    q.s.reserve_epoch_check()
    assert q.meta() == ("peer", "follower", e + 1), f"старое слово второй стороны вернуло эпоху: {q.meta()}"
