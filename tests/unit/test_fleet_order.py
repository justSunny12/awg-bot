"""Набор обновлений, ход вперёд: «сначала сервер», `needs_gw` — сначала шлюзы,
цепочка минимумов у сервера, шлюзы по одному (резервные, активный
последним), шаги хода по порядку, фиксация.

Цена ошибки: команда шлюзу раньше ступени сервера — агент с `needs_main`
откажет, и набор откатится зря; два шлюза разом — плохой выпуск роняет все
слоты; фиксация без `upd_commit` — прежний код вечно на диске шлюза;
следующий шаг до факта предыдущего — гонка «ждали факта» (правило проекта).
"""
from __future__ import annotations

import pytest

from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.unit

BOTH = {"requires": "3.4.0"}


def test_the_server_goes_first_and_gateways_wait_for_its_new_process(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    fleet.slot(2)
    assert fleet.start() == ""
    out = fleet.tick()
    assert out.server == "apply" and out.step.tag == "v3.5.0" and fleet.sent(out) == []
    assert fleet.run()["steps"] == [["main", "v3.5.0"]]
    fleet.s.fleet_server_act(out)
    assert fleet.applied == [("v3.5.0", True)], "ступень сервера без удержания прежнего"
    # новый процесс поднялся, но ещё не вышел на связь (сторож взведён) — шлюзам ничего
    fleet.server("3.5.0")
    fleet.guard(True)
    assert fleet.tick().send == [], "команда шлюзу раньше, чем сервер вышел на связь"
    fleet.guard(False)
    out = fleet.tick()
    assert [s for s, _ in fleet.sent(out)] == [2], "первым — резервный (активный — слот 1)"
    body = fleet.sent(out)[0][1]
    assert body["target"] == "v3.5.0" and body["hold"] is True and body["why"] == "fleet"
    assert body["digest"].startswith("sha256:")


def test_gateways_go_one_by_one_and_the_steps_are_written_in_order(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    for n in (1, 2, 3):
        fleet.slot(n)
    fleet.active(1)
    fleet.start()
    out = fleet.tick()
    assert [s for s, _ in fleet.sent(out)] == [2]
    assert fleet.tick().send == [], "второй шлюз получил команду, пока первый в пути"
    out = fleet.gw_done(2, "3.4.1")
    assert [s for s, _ in fleet.sent(out)] == [3]
    out = fleet.gw_done(3, "3.4.1")
    assert [s for s, _ in fleet.sent(out)] == [1], "активный — последним"
    out = fleet.gw_done(1, "3.4.1")
    run = fleet.run()
    assert run["steps"] == [["gw", 2, "v3.4.1"], ["gw", 3, "v3.4.1"], ["gw", 1, "v3.4.1"]]
    assert run["state"] == "done" and out.final
    assert sorted(s for s, _ in fleet.sent(out, "upd_commit")) == [1, 2, 3]
    assert out.server == "", "сервер не обновлялся — фиксировать у него нечего"


def test_needs_gw_puts_the_gateways_before_the_server(fleet):
    fleet.releases(rel("v3.4.0", both=True),
                   rel("v3.5.0", main={"requires": "3.4.0", "needs_gw": "3.5.0"}, gw=BOTH))
    fleet.slot(1)
    fleet.start()
    out = fleet.tick()
    assert out.server == "" and [s for s, _ in fleet.sent(out)] == [1], "сервер раньше нужных ему шлюзов"
    out = fleet.gw_done(1, "3.5.0")
    assert out.server == "apply" and out.step.tag == "v3.5.0"
    assert fleet.run()["steps"] == [["gw", 1, "v3.5.0"], ["main", "v3.5.0"]]
    out = fleet.finish_server(out, "3.5.0")
    assert fleet.run()["state"] == "done" and out.server == "commit"
    assert [s for s, _ in fleet.sent(out, "upd_commit")] == [1]


def test_an_unavailable_gateway_does_not_hold_needs_gw(fleet):
    """Р22: шлюз не на связи `needs_gw` не держит — сервер идёт сам."""
    fleet.releases(rel("v3.4.0", both=True),
                   rel("v3.5.0", main={"requires": "3.4.0", "needs_gw": "3.5.0"}, gw=BOTH))
    fleet.slot(1, online=False)
    fleet.start()
    out = fleet.tick()
    assert out.server == "apply" and fleet.sent(out) == []


def test_the_server_walks_its_chain_of_minimums_step_by_step(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.5", both=True),
                   rel("v3.5.0", main={"requires": "3.4.5"}, gw=BOTH))
    fleet.start()
    out = fleet.tick()
    assert out.step.tag == "v3.4.5"
    out = fleet.finish_server(out, "3.4.5")
    assert out.server == "apply" and out.step.tag == "v3.5.0"
    out = fleet.finish_server(out, "3.5.0")
    assert fleet.run()["steps"] == [["main", "v3.4.5"], ["main", "v3.5.0"]]
    assert fleet.run()["state"] == "done" and out.server == "commit"


def test_a_held_gateway_that_was_away_at_the_commit_gets_it_on_hello(fleet):
    """Фиксация: доступным — upd_commit сразу, пропавшему — по hello."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    fleet.active(1)
    fleet.start()
    fleet.tick()
    fleet.gw_done(2, "3.4.1")
    fleet.offline(2)
    out = fleet.gw_done(1, "3.4.1")
    assert [s for s, _ in fleet.sent(out, "upd_commit")] == [1]
    assert fleet.st(2)["hold"] is True
    out = fleet.hello(2, "3.4.1")
    assert [s for s, _ in fleet.sent(out, "upd_commit")] == [2] and fleet.st(2)["hold"] is False


def test_start_refuses_a_running_fleet_and_a_changed_target(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    assert fleet.s.fleet_start("v3.9.9") == "changed"
    assert fleet.start() == ""
    assert fleet.start() == "running"
    fleet.releases(rel("v3.4.0", both=True))
    fleet.s.fleet_run_set({})
    assert fleet.start() == "nothing"


def test_a_gateway_already_on_the_target_is_not_commanded(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", both=True))
    fleet.slot(1, "3.4.1")
    fleet.start()
    out = fleet.tick()
    out = fleet.finish_server(out, "3.4.1")
    assert fleet.sent(out) == [] and fleet.run()["state"] == "done"


def test_the_server_update_note_waits_for_the_live_message(fleet):
    """Итог своего обновления после перезапуска в ходе — без отдельного
    сообщения: о нём говорит живое сообщение набора."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.start()
    assert fleet.s.update_applied_note("v3.5.0", True) is None
    fleet.s.fleet_run_set({**fleet.run(), "state": "done"})
    assert fleet.s.update_applied_note("v3.5.0", True) is ...
