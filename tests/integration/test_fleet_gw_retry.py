"""«🔁 Повторить» на уведомлении о неудачном догоне одного шлюза: уведомление
несёт подсказку действия (gw_retry, слот), fleet_gw_retry сбрасывает
расписание повтора — команда уходит ближайшим тактом, а не через 30 минут /
2 часа / 6 часов; команда `upd` везёт число прошлых неудач (tries), чтобы
агент сам сказал, когда сервер повторит.

Цена ошибки: без кнопки админ после «не обновился — повторю через 6 часов»
ждёт шесть часов или идёт в раздел обновлений; без tries агент пишет
«повторит через 30 минут» перед каждой из трёх попыток; повтор у лишённого
кворума — команда шлюзу от сервера, который сейчас решений не принимает.
"""
from __future__ import annotations

import datetime

import pytest

from awgbot.util import timeutil
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.integration

BOTH = {"requires": "3.4.0"}


def _failed_catchup(fleet, monkeypatch):
    """Набор зафиксирован без шлюза 1; он вышел на связь, догон не прошёл —
    слот ждёт повтора через 30 минут."""
    import random
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1, online=False)
    fleet.start()
    assert fleet.tick().send == [] and fleet.run()["state"] == "done"
    monkeypatch.setattr(random, "uniform", lambda lo, hi: 1.0)        # без джиттера
    fleet.hello(1, "3.4.0")
    return fleet.ack(1, "failed", error="GitHub не ответил")


# ── уведомление несёт действие ───────────────────────────────────────────────

def test_a_failed_catch_up_note_offers_the_retry_for_that_slot(fleet, monkeypatch):
    out = _failed_catchup(fleet, monkeypatch)
    assert len(out.notes) == 1, out.notes
    note = out.notes[0]
    assert note.text.endswith("— повторю через 30 минут"), note.text
    assert note.action == ("gw_retry", 1), f"уведомление о неудачном догоне без «🔁 Повторить»: {note.action}"
    assert note.silent


def test_the_given_up_note_offers_the_retry_too(fleet, monkeypatch):
    """После третьей неудачи повторов по расписанию нет — кнопка тем более
    нужна: иначе остаётся только раздел обновлений."""
    _failed_catchup(fleet, monkeypatch)
    for _ in range(3):
        at = timeutil.parse_iso(fleet.st(1)["next_at"])
        monkeypatch.setattr(timeutil, "now", lambda at=at: at + datetime.timedelta(seconds=1))
        assert [s for s, _ in fleet.sent(fleet.tick())] == [1]
        out = fleet.ack(1, "failed", error="GitHub не ответил")
    assert fleet.st(1)["final"] is True
    assert "повторов больше не будет" in out.notes[0].text, out.notes[0].text
    assert out.notes[0].action == ("gw_retry", 1), out.notes[0].action


# ── fleet_gw_retry ───────────────────────────────────────────────────────────

def test_retry_sends_the_command_on_the_next_tick_instead_of_waiting_for_the_schedule(fleet, monkeypatch):
    _failed_catchup(fleet, monkeypatch)
    st = fleet.st(1)
    assert st["tries"] == 1 and st["next_at"], st
    assert fleet.tick().send == [], "повтор ушёл раньше срока и без кнопки"
    assert fleet.s.fleet_gw_retry(1) == ""
    st = fleet.st(1)
    assert (st["tries"], st["next_at"], st["final"]) == (0, "", False), f"расписание повтора не сброшено: {st}"
    sent = fleet.sent(fleet.tick())
    assert [s for s, _ in sent] == [1], "после «🔁 Повторить» команда не ушла ближайшим тактом"
    assert sent[0][1]["target"] == "v3.4.1" and sent[0][1]["why"] == "catchup", sent[0][1]
    assert sent[0][1]["tries"] == 0, "после сброса агенту ушёл счёт прошлых неудач"
    assert fleet.st(1)["state"] == "sent"


def test_retry_after_the_retries_are_exhausted_starts_the_catch_up_again(fleet, monkeypatch):
    _failed_catchup(fleet, monkeypatch)
    for _ in range(3):
        at = timeutil.parse_iso(fleet.st(1)["next_at"])
        monkeypatch.setattr(timeutil, "now", lambda at=at: at + datetime.timedelta(seconds=1))
        fleet.tick()
        fleet.ack(1, "failed", error="GitHub не ответил")
    assert fleet.st(1)["final"] is True and fleet.tick().send == []
    assert fleet.s.fleet_gw_retry(1) == ""
    assert fleet.st(1)["final"] is False
    assert [s for s, _ in fleet.sent(fleet.tick())] == [1], "исчерпанные повторы не возобновились кнопкой"


def test_a_second_press_while_the_command_is_on_its_way_is_nothing(fleet, monkeypatch):
    """Команда уже ушла (слот «sent») — повтор не плодит вторую команду и не
    сбрасывает счёт."""
    _failed_catchup(fleet, monkeypatch)
    assert fleet.s.fleet_gw_retry(1) == "" and fleet.s.fleet_gw_retry(1) == "", "повторное нажатие до такта"
    fleet.tick()
    assert fleet.s.fleet_gw_retry(1) == "nothing"
    assert fleet.tick().send == [], "вторая команда тому же шлюзу"


def test_retry_is_nothing_for_a_slot_without_a_failure(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    assert fleet.s.fleet_gw_retry(1) == "nothing", "шлюз без неудачи «ждёт повтора»"
    assert fleet.s.fleet_gw_retry(7) == "nothing", "несуществующий слот"
    assert fleet.tick().send == []


def test_retry_is_running_while_a_run_is_active(fleet):
    """Идёт ход набора — команды шлёт только он; кнопка с прежнего уведомления
    говорит «уже идёт» и ничего не трогает."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    fleet.s.fleet_slot_set(2, {"id": "a1b2c3d4", "state": "failed", "target": "v3.4.1", "tries": 2,
                               "final": False, "next_at": "2030-01-01T00:00:00+00:00", "why": "catchup"})
    fleet.start()
    assert fleet.run()["state"] == "running"
    assert fleet.s.fleet_gw_retry(2) == "running"
    assert fleet.st(2)["tries"] == 2 and fleet.st(2)["next_at"], "во время хода счёт повторов сброшен"


def test_a_slot_failed_inside_a_run_is_not_a_catch_up_to_retry(fleet):
    """Неудача шага хода — дело отката набора и «🔁 Повторить» в итоге хода,
    не кнопки одного шлюза."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.start()
    fleet.tick()
    fleet.ack(1, "failed", error="GitHub не ответил")
    assert fleet.run()["state"] in ("rolling_back", "rolled_back", "rollback_failed"), fleet.run()
    fleet.s.fleet_run_set({**fleet.run(), "state": "rolled_back"})
    assert fleet.st(1).get("run"), "слот хода без отметки хода"
    assert fleet.s.fleet_gw_retry(1) == "nothing"


# ── tries в команде ──────────────────────────────────────────────────────────

def test_the_command_carries_the_number_of_past_failures(fleet, monkeypatch):
    """Первая команда — tries 0; повтор после неудачи — 1, 2, 3: по нему агент
    скажет «повторит через 2 часа», а не всегда «через 30 минут»."""
    import random
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1, online=False)
    fleet.start()
    fleet.tick()
    monkeypatch.setattr(random, "uniform", lambda lo, hi: 1.0)
    out = fleet.hello(1, "3.4.0")
    assert fleet.sent(out)[0][1]["tries"] == 0, fleet.sent(out)
    for want in (1, 2, 3):
        fleet.ack(1, "failed", error="GitHub не ответил")
        at = timeutil.parse_iso(fleet.st(1)["next_at"])
        monkeypatch.setattr(timeutil, "now", lambda at=at: at + datetime.timedelta(seconds=1))
        sent = fleet.sent(fleet.tick())
        assert [s for s, _ in sent] == [1]
        assert sent[0][1]["tries"] == want, f"повтор №{want} несёт tries {sent[0][1]['tries']}"


@pytest.mark.parametrize("how", ["mark", "journal"])
def test_retry_while_fenced_resets_nothing(fleet, monkeypatch, how):
    """Лишённый кворума (по отметке такта или по журналу — после перезапуска,
    пока такт не прошёл): «fenced», расписание повтора не тронуто."""
    _failed_catchup(fleet, monkeypatch)
    before = dict(fleet.st(1))
    if how == "mark":
        fleet.s.__dict__["_quo_fenced"] = True
    else:
        fleet.s.db.jrn_fence(why="DUMMY")
    assert fleet.s.fleet_gw_retry(1) == "fenced"
    assert fleet.st(1) == before, "у лишённого кворума сброшено расписание повтора"


def test_upd_body_has_tries_as_an_int_even_when_the_slot_has_none():
    from awgbot.domain.fleet.order import _upd_body
    body = _upd_body({"id": "a1b2c3d4", "target": "v3.4.1", "why": "catchup", "tries": 2})
    assert body["tries"] == 2 and body["why"] == "catchup" and body["hold"] is False, body
    assert _upd_body({"id": "a1b2c3d4", "target": "v3.4.1"})["tries"] == 0
