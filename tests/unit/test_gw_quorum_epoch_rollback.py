"""Агент шлюза — эпоха, поднятая по `qtake`, против факта
(domain/gateway/reserve/quorum.py, store.py; util/quorum/voter.py): «забираю»
кандидата поднимает эпоху голосующего до e + 1 словом, не фактом; сорванное
«забираю» (`qrel{e}` кандидата, `state{idle, e}` резерва — моих у него нет)
возвращает её к факту и на диске (`e_take` — отдельно от факта `e_known`) тоже; эпоха, подтверждённая
ответчиком или `qstate`, назад не откатывается.

Мир — агент шлюза 1 линии `a` из tests/unit/test_gw_quorum_decision.py:
подмены интерфейсов зондов и файлов, пробы и ответы — настоящие датаграммы,
часы — модельные.

Цена ошибки: шлюзы после сорванного «забираю» навсегда под e + 1 — живая
основа принимает эту эпоху по первой же пробе и уходит в away по принятой
эпохе, бот молчит пользователям, которые никуда не уезжали; после перезапуска
агента то же повторяла бы запись с диска.
"""
from __future__ import annotations

import pytest

from tests.unit.test_gw_quorum_decision import E, NAME, ag, clock  # noqa: F401

pytestmark = pytest.mark.unit

__all__ = ["ag", "clock"]


def test_qrel_after_qtake_rolls_the_raised_epoch_back_to_the_fact(ag):
    ag.see()
    ag.uns()
    ag.word("qtake", e=E)
    assert ag.v.epoch == E + 1 and ag.section().get("e_take") == E + 1, "подготовка: qtake не поднял эпоху"
    ag.word("qrel", e=E)
    assert ag.v.epoch == E, "qrel после сорванного «забираю» оставил эпоху e + 1"
    assert not ag.section().get("e_take") and int(ag.section().get("e_known") or 0) <= E, "поднятая эпоха осталась на диске — после перезапуска снова e + 1"
    acts = ag.run(60, answer=True)
    assert ag.probes(acts)[-1].e == E, "пробы основе после qrel идут с эпохой e + 1"


def test_the_idle_word_of_the_reserve_rolls_the_raised_epoch_back(ag):
    ag.see()
    ag.uns()
    ag.word("qtake", e=E)
    ag.svc.reserve_guest_in("state", {"state": "idle", "move": 3, "e": E})
    assert ag.v.epoch == E, "резерв сказал idle под e, а голосующий держит e + 1"
    assert not ag.section().get("e_take") and int(ag.section().get("e_known") or 0) <= E


def test_an_epoch_confirmed_by_the_responder_is_not_rolled_back(ag):
    ag.see()
    ag.uns()
    ag.word("qtake", e=E)
    acts = ag.run(40, answer=True, e=E + 1)                    # ответчик (клон или новый лидер) подтвердил e + 1 — факт
    assert ag.probes(acts), "подготовка: за 40 с ни одной пробы"
    assert ag.v.fact == E + 1, "подготовка: ответ с e + 1 не стал фактом"
    ag.word("qrel", e=E)
    assert ag.v.epoch == E + 1, "qrel откатил эпоху, подтверждённую ответчиком"
    ag.svc.reserve_guest_in("state", {"state": "idle", "move": 3, "e": E})
    assert ag.v.epoch == E + 1, "idle со старой эпохой откатил подтверждённую"
    assert ag.section().get("e_known") == E + 1


def test_a_plain_qrel_of_the_pledge_keeps_the_epoch(ag):
    ag.see()
    ag.uns()
    ag.word("qrel", e=E)
    assert ag.v.epoch == E and NAME not in ag.pledges()


def test_the_rollback_survives_an_agent_restart(ag):
    ag.see()
    ag.uns()
    ag.word("qtake", e=E)
    ag.word("qrel", e=E)
    svc = ag.new()
    svc.quorum_tick(ag.clock.t)
    assert svc._gw_qvoters[NAME].epoch == E, "после перезапуска агент снова под e + 1 (e_known с диска)"


def test_a_restart_between_qtake_and_qrel_still_rolls_the_epoch_back(ag):
    """Поднятая по qtake эпоха лежит на диске отдельно от факта (e_take): после перезапуска
    агента голосующий снова под e + 1, но qrel её снимает — иначе e + 1 стала бы фактом навсегда."""
    ag.see()
    ag.uns()
    ag.word("qtake", e=E)
    svc = ag.new()
    svc.quorum_tick(ag.clock.t)
    v = svc._gw_qvoters[NAME]
    assert v.epoch == E + 1 and v.fact == E, f"после перезапуска поднятая эпоха стала фактом: epoch {v.epoch}, fact {v.fact}"
    svc.quorum_guest_in("qrel", {"l": "a", "e": E})
    assert v.epoch == E, "qrel после перезапуска не вернул эпоху к факту"
    svc2 = ag.new()
    svc2.quorum_tick(ag.clock.t)
    assert svc2._gw_qvoters[NAME].epoch == E, "после второго перезапуска эпоха снова e + 1"


def test_qtake_before_the_first_tick_after_a_restart_is_rolled_back_by_qrel(ag):
    svc = ag.new()                                             # голосующего ещё нет: слово ложится на диск
    svc.quorum_guest_in("qtake", {"l": "a", "e": E})
    svc.quorum_tick(ag.clock.t)
    v = svc._gw_qvoters[NAME]
    assert v.epoch == E + 1 and v.fact == E
    svc.quorum_guest_in("qrel", {"l": "a", "e": E})
    assert v.epoch == E, "qtake до первого такта не откатился по qrel"


def test_a_restart_between_qtake_and_the_idle_word_still_rolls_back(ag):
    ag.see()
    ag.uns()
    ag.word("qtake", e=E)
    svc = ag.new()
    svc.quorum_tick(ag.clock.t)
    svc.reserve_guest_in("state", {"state": "idle", "move": 3, "e": E})
    assert svc._gw_qvoters[NAME].epoch == E, "idle резерва после перезапуска не вернул эпоху к факту"
    assert not ag.section().get("e_take"), "поднятая эпоха осталась на диске"


def test_a_fact_at_or_above_the_raised_epoch_clears_it_on_disk(ag):
    ag.see()
    ag.uns()
    ag.word("qtake", e=E)
    assert ag.section().get("e_take") == E + 1
    ag.run(40, answer=True, e=E + 1)                           # ответчик подтвердил e + 1 — факт
    assert ag.section().get("e_known") == E + 1 and not ag.section().get("e_take")
