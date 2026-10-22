"""`fleet_retry` после отката, когда цель ушла вперёд: ход до v1.2.0
откатился, вышла v1.3.0 — «🔁 Повторить» отказывает «changed» (обработчик
покажет «Цель обновления изменилась…»), ход не стартует и итог прошлого
хода не тронут; цель та же — ход заново, как прежде. Откат не прошёл —
повтор идёт к текущей цели: до него сервер AWG шлюзам команд не шлёт, и
держать его ради смены цели нельзя.

Цена ошибки: админ нажимает «Повторить» ради v1.2.0, а набор уходит на
v1.3.0, которую он не выбирал и чей список изменений не видел.
"""
from __future__ import annotations

import pytest

from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.integration

ROLE = {"requires": "1.0.0"}
V_BASE, V_OLD, V_NEW = "v1.1.0", "v1.2.0", "v1.3.0"


def _rolled_back(fleet, *, newer: bool):
    """Сервер и шлюз на v1.1.0; ход до v1.2.0 откатился (шлюз не обновился);
    newer — после отката в списке появилась v1.3.0."""
    fleet.server("1.1.0")
    fleet.releases(rel(V_BASE, main=ROLE, gw=ROLE), rel(V_OLD, main=ROLE, gw=ROLE))
    fleet.slot(1, version="1.1.0")
    assert fleet.start() == "" and fleet.run()["tag"] == V_OLD, "сценарий собран не так: ход не начат"
    fleet.finish_server(fleet.tick(), "1.2.0")
    out = fleet.ack(1, "failed", error="сеть")
    assert out.server == "revert", out
    fleet.s.fleet_server_act(out)
    fleet.server("1.1.0")
    fleet.tick()
    assert fleet.run()["state"] == "rolled_back", fleet.run()
    if newer:
        fleet.releases(rel(V_BASE, main=ROLE, gw=ROLE), rel(V_OLD, main=ROLE, gw=ROLE),
                       rel(V_NEW, main=ROLE, gw=ROLE))
    assert fleet.s.fleet_target().tag == (V_NEW if newer else V_OLD)


def test_retry_after_a_rollback_refuses_when_the_target_moved_on(fleet, monkeypatch):
    _rolled_back(fleet, newer=True)
    before = fleet.run()
    starts: list = []
    monkeypatch.setattr(fleet.s, "fleet_start", lambda tag: starts.append(tag) or "")
    assert fleet.s.fleet_retry() == "changed", "повтор пошёл к цели, которую человек не выбирал"
    assert starts == [], f"ход начат при смене цели: {starts}"
    assert fleet.run() == before, "итог откатившегося хода тронут отказом"
    assert fleet.tick().send == [] and fleet.applied == [(V_OLD, True)], "после отказа набор зашевелился сам"


def test_retry_after_a_rollback_starts_again_when_the_target_is_the_same(fleet):
    _rolled_back(fleet, newer=False)
    assert fleet.s.fleet_retry() == ""
    run = fleet.run()
    assert run["state"] == "running" and run["tag"] == V_OLD and run["steps"] == [], run
    assert fleet.tick().server == "apply", "повтор к той же цели не позвал апдейтер сервера"


def test_retry_after_a_failed_rollback_goes_to_the_current_target(fleet):
    """Откат не прошёл: шлюзам команд нет, пока не нажмут «🔁 Повторить» —
    повтор не ждёт прежней цели, идёт к той, что доступна."""
    _rolled_back(fleet, newer=True)
    fleet.s.fleet_run_set({**fleet.run(), "state": "rollback_failed"})
    assert fleet.s.fleet_retry() == ""
    run = fleet.run()
    assert run["state"] == "running" and run["tag"] == V_NEW, run
    out = fleet.tick()
    assert out.server == "apply"
    fleet.s.fleet_server_act(out)
    assert fleet.applied[-1] == (V_NEW, True), f"апдейтеру ушла не текущая цель: {fleet.applied}"
