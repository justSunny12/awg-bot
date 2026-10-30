"""Обязательство «нет» голосующего снимается, когда линия ушла за его эпоху
(util/quorum/voter.py: Voter.tick → `unpledge`), и у агента шлюза запись обязательства
на диске уходит по этому `unpledge` (domain/gateway/reserve/quorum.py).

Машина — без ввода-вывода, время — аргумент; агент — на временных файлах
(`_Agent` из tests/unit/test_gw_quorum_decision.py).

Цена ошибки: после состоявшегося переезда (клон или новый лидер ответил эпохой больше)
шлюз до срока обязательства шлёт `held` вместо «вижу» — возврат «сразу» по Q2 закрыт
до 10 минут; запись на диске остаётся — после перезапуска агента обязательство
возвращается снова.
"""
from __future__ import annotations

import pytest

from awgbot.util.quorum.timing import Timing
from awgbot.util.quorum.voter import Voter
from tests.unit.test_gw_quorum_decision import E, NAME, ag, clock

pytestmark = pytest.mark.unit
__all__ = ["ag", "clock"]

T0 = 1000.0


def _pledged(e: int = 7) -> Voter:
    v = Voter("a", "a1", timing=Timing())
    v.epoch_in(e)
    v.view = "see"
    v.said_no(T0)
    assert v.w_for_probe() == "held", "подготовка: обязательства нет"
    return v


def test_a_bigger_epoch_lifts_the_pledge_before_its_term():
    v = _pledged()
    v.epoch_in(8)
    assert "unpledge" in v.tick(T0 + 1), "линия ушла за эпоху обязательства — оно держится до срока"
    assert v.pledge is None and v.w_for_probe() == "see", "после снятия пробы всё ещё held"


def test_the_same_epoch_keeps_the_pledge_until_its_term():
    v = _pledged()
    v.epoch_in(7)
    assert "unpledge" not in v.tick(T0 + 1) and v.pledge is not None, "та же эпоха сняла обязательство"


def test_the_own_raised_epoch_lifts_the_pledge_too():
    """Своё «забираю» (raise_to) — тоже уход линии за эпоху обязательства."""
    v = _pledged()
    v.raise_to(8)
    assert "unpledge" in v.tick(T0 + 1)


def test_the_unpledge_is_said_once():
    v = _pledged()
    v.epoch_in(8)
    v.tick(T0 + 1)
    assert "unpledge" not in v.tick(T0 + 2), "unpledge повторяется"


def test_the_agent_drops_the_pledge_on_disk_when_the_line_moves_on(ag):
    ag.see()
    ag.uns()
    assert ag.pledges().get(NAME, {}).get("e") == E, "подготовка: обязательство не записано"
    acts = ag.run(60, answer=True, e=E + 1)                  # ответил клон под эпохой больше
    assert NAME not in ag.pledges(), "линия ушла за эпоху обязательства, а запись на диске осталась"
    ws = [p.w for p in ag.probes(acts)]
    assert ws and ws[-1] == "see", f"после ухода линии пробы всё ещё held: {ws}"


def test_the_agent_keeps_the_pledge_while_the_epoch_is_the_same(ag):
    ag.see()
    ag.uns()
    ag.run(60, answer=True)
    assert ag.pledges().get(NAME, {}).get("e") == E, "та же эпоха сняла обязательство раньше срока"
