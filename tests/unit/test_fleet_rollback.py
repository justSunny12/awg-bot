"""Набор обновлений, откат (Р21): неудача любого доступного шлюза — назад весь
набор, порядок отката — обратный ходу; откат локальный; откат, не прошедший
на хосте, — `rollback_failed` без автоматических команд; сторож сервера
откатил сервер — Т-У8; «🔁 Повторить» — ход заново.

Инвариант: ни одного состояния после хода, где доступные хосты на разных
выпусках, — либо все на новом, либо все на прежнем.
"""
from __future__ import annotations

import json

import pytest

from awgbot.bot.texts import fleet as ft
from awgbot.infra import updates
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.unit

BOTH = {"requires": "3.4.0"}


def _three(fleet, target=None):
    """Сервер и три шлюза; активный — слот 1 (очередь: 2, 3, 1)."""
    fleet.releases(rel("v3.4.0", both=True), target or rel("v3.5.0", both=True))
    for n in (1, 2, 3):
        fleet.slot(n)
    fleet.active(1)
    assert fleet.start() == ""
    out = fleet.tick()
    return fleet.finish_server(out, "3.5.0")


def _no_github(monkeypatch):
    def boom(**_):
        raise AssertionError("откат пошёл на GitHub")
    monkeypatch.setattr(updates, "list_releases", boom)


@pytest.mark.parametrize("state", ["failed", "rolled_back", "refused"])
@pytest.mark.parametrize("place", ["first", "middle", "last"])
def test_a_failure_anywhere_rolls_the_whole_fleet_back_in_reverse(fleet, monkeypatch, state, place):
    _three(fleet)
    _no_github(monkeypatch)
    done = {"first": [], "middle": [2], "last": [2, 3]}[place]
    failing = {"first": 2, "middle": 3, "last": 1}[place]
    for n in done:
        fleet.gw_done(n, "3.5.0")
    out = fleet.ack(failing, state, error="обновленный агент не вышел на связь за 10 минут")
    run = fleet.run()
    assert run["state"] == "rolling_back" and run["failed_on"] == failing
    order = []
    while True:
        reverts = fleet.sent(out, "upd_revert")
        if reverts:
            assert len(reverts) == 1, "откат шлюзов — не по одному"
            slot, body = reverts[0]
            assert body["to"] == "v3.4.0"
            order.append(slot)
            fleet.online(slot, "3.4.0")
            out = fleet.ack(slot, "reverted")
            continue
        if out.server == "revert":
            order.append("main")
            fleet.s.fleet_server_act(out)
            assert fleet.spawned[-1] == ["__update_revert"]
            fleet.server("3.4.0")                         # новый (прежний) процесс продолжает откат
            out = fleet.tick()
            continue
        break
    assert order == list(reversed(done)) + ["main"], "откат не в обратном ходу порядке"
    assert fleet.run()["state"] == "rolled_back" and out.final
    assert fleet.tick().send == [], "после отката набор снова кого-то двигает"


def test_needs_gw_rolls_back_server_first_then_gateways(fleet, monkeypatch):
    """Ход с needs_gw («шлюзы, затем сервер») откатывается «сервер, затем
    шлюзы»: ни на одном шаге нет старого сервера с новыми агентами там, где
    ход вперёд их не допускал."""
    fleet.releases(rel("v3.4.0", both=True),
                   rel("v3.5.0", main={"requires": "3.4.0", "needs_gw": "3.5.0"}, gw=BOTH))
    fleet.slot(1)
    fleet.start()
    fleet.tick()
    out = fleet.gw_done(1, "3.5.0")
    out = fleet.finish_server(out, "3.5.0")
    assert fleet.run()["state"] == "done"
    # другой ход: поздний шлюз посреди хода с needs_gw и его неудача после сервера
    fleet.releases(rel("v3.4.0", both=True),
                   rel("v3.5.0", main={"requires": "3.4.0", "needs_gw": "3.5.0"}, gw=BOTH),
                   rel("v3.6.0", main={"requires": "3.4.0", "needs_gw": "3.6.0"}, gw=BOTH))
    fleet.s.fleet_run_set({})
    fleet.start()
    out = fleet.tick()
    out = fleet.gw_done(1, "3.6.0")
    assert out.server == "apply"
    fleet.slot(2, "3.5.0")                                    # вышел на связь посреди хода
    out = fleet.finish_server(out, "3.6.0")
    assert [s for s, _ in fleet.sent(out)] == [2]
    _no_github(monkeypatch)
    out = fleet.ack(2, "failed", error="sha256 не совпал")
    assert out.server == "revert" and fleet.sent(out, "upd_revert") == [], "сервер — первым"
    fleet.s.fleet_server_act(out)
    fleet.server("3.5.0")
    out = fleet.tick()
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [1]
    fleet.online(1, "3.5.0")
    out = fleet.ack(1, "reverted")
    assert fleet.run()["state"] == "rolled_back"


def test_a_revert_that_fails_on_one_host_leaves_rollback_failed_and_no_commands(fleet):
    _three(fleet)
    fleet.gw_done(2, "3.5.0")
    fleet.gw_done(3, "3.5.0")
    out = fleet.ack(1, "failed", error="место кончилось")
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [3]
    out = fleet.ack(3, "revert_failed", error="прежней версии нет")
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [2], "остальные не откатываются дальше"
    fleet.online(2, "3.4.0")
    out = fleet.ack(2, "reverted")
    fleet.s.fleet_server_act(out)
    fleet.server("3.4.0")
    out = fleet.tick()
    run = fleet.run()
    assert run["state"] == "rollback_failed" and run["revert_failed"] == [3]
    # ни догона, ни следования: шлюз 3 на v3.5.0 при сервере v3.4.0 — без команд
    assert fleet.tick().send == []
    assert fleet.hello(3, "3.5.0").send == [], "в rollback_failed сервер тянет шлюз сам"
    text = ft.fleet_live(fleet.s.fleet_run_plan())
    assert text.startswith("⚠️ <b>Откат набора не удался</b>: «Шлюз 3» остался на v3.5.0 — прежней версии нет")
    assert "Пока не нажмёшь «🔁 Повторить», сервер AWG шлюзам команд не шлёт" in text


def test_the_server_without_its_old_version_is_a_failed_revert_too(fleet):
    _three(fleet)
    fleet.revert_rc = 2
    out = fleet.ack(2, "failed", error="сеть")
    assert out.server == "revert"
    out = fleet.s.fleet_server_act(out)
    assert fleet.run()["state"] == "rollback_failed" and fleet.run()["revert_failed"] == ["main"]
    assert "__update_revert" not in [a[0] for a in fleet.spawned]


def test_the_server_guard_rolling_the_server_back_ends_the_run(fleet):
    """Обновлённый сервер не вышел на связь — сторож вернул прежний; шлюзам в
    обычном ходе команд ещё не было: набор rolled_back, Т-У8 без звука."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    fleet.start()
    out = fleet.tick()
    fleet.s.fleet_server_act(out)
    (fleet.tmp / "update-rolled-back").write_text(json.dumps({"version": "3.5.0", "prev": "3.4.0"}),
                                                  encoding="utf-8")
    out = fleet.s.fleet_on_start()
    run = fleet.run()
    assert run["state"] == "rolled_back" and run["failed_on"] == "main" and fleet.sent(out) == []
    assert out.notes and out.notes[0].silent
    assert out.notes[0].text == ("⚠️ Обновление до v3.5.0 отменено: обновлённый сервер AWG не вышел на связь за "
                                 "10 минут — вернул v3.4.0. Журнал: <code>journalctl -u awg-bot -o short-iso</code>")
    assert not (fleet.tmp / "update-rolled-back").exists()


def test_a_server_step_that_does_not_start_rolls_back_what_was_done(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.start()
    out = fleet.tick()

    def boom(r, hold=False):
        raise updates.UpdateError("GitHub HTTP 500")
    fleet.mp.setattr(fleet.s, "apply_update", boom)
    out = fleet.s.fleet_server_act(out)
    run = fleet.run()
    assert run["state"] == "rolled_back" and run["steps"] == [] and "GitHub HTTP 500" in run["reason"]


def test_retry_starts_the_whole_fleet_again_from_what_stands(fleet):
    _three(fleet)
    out = fleet.ack(2, "refused", error="занят")
    fleet.s.fleet_server_act(out)
    fleet.server("3.4.0")
    fleet.tick()
    assert fleet.run()["state"] == "rolled_back"
    text = ft.fleet_live(fleet.s.fleet_run_plan())
    assert text.startswith("⚠️ <b>Обновление до v3.5.0 отменено</b> — набор вернулся на v3.4.0")
    assert "⚠️ «Шлюз 2» — обновление не удалось, вернул v3.4.0: занят" in text
    assert "↩️ Сервер AWG — вернул v3.4.0" in text
    assert fleet.s.fleet_retry() == ""
    run = fleet.run()
    assert run["state"] == "running" and run["steps"] == [] and run["tag"] == "v3.5.0"
    assert fleet.tick().server == "apply"


def test_no_state_offers_to_go_on_without_a_gateway(fleet):
    """Кнопки «продолжить без шлюза» нет ни в одном итоге (Р21)."""
    from awgbot.bot import keyboards as kb
    for retry in (False, True):
        labels = [b.text for row in kb.fleet_final(retry).inline_keyboard for b in row]
        assert labels == (["🔁 Повторить", "⬅️ В меню"] if retry else ["⬅️ В меню"])
