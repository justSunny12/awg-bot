"""Набор обновлений: сценарии ревью этапов 0–1, которые раньше ломали ход.

- шаг сервера отмечен до апдейтера: такт, пришедший, пока апдейтер собирает
  ядро, второго `awg-bot update` не зовёт; факт — новый процесс с ожидаемой
  версией; апдейтер умер или 20 минут — откат набора (logic-2);
- такт по расписанию — только когда есть дело (logic-1, «вне оси»);
- шлюз на связи без ответа полчаса — неудача шага (logic-3);
- откат сторожем снимает только последнюю ступень сервера (logic-4);
- ступени сервера, перемежённые шлюзами, откатываются одним откатом сервера
  на версию до хода (logic-5);
- команда вне хода у шлюза на связи держит ход; не начатая — снимается, ход
  даст свою (logic-7);
- удержание сменившегося хода снимается `upd_commit` по id его команды;
  вернувшийся посреди шага — «done» только по `upd_ack` (logic-8, logic-13);
- ручное обновление сервера — «follow», а не догон (вне оси).
"""
from __future__ import annotations

import os

import pytest

from awgbot.domain.fleet import server as fleet_server
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.unit

BOTH = {"requires": "3.4.0"}


def _two(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    fleet.slot(2)
    fleet.active(1)
    assert fleet.start() == ""


# ── logic-2: шаг сервера и его факт ─────────────────────────────────────────

def test_a_tick_while_the_updater_works_does_not_start_a_second_one(fleet):
    _two(fleet)
    out = fleet.tick()
    assert out.server == "apply"
    fleet.s.fleet_server_act(out)
    assert fleet.hold_ids == [fleet.run()["id"]], "удержание сервера без id хода"
    for _ in range(3):
        again = fleet.tick()
        assert again.server == "" and again.send == [], "второй апдейтер посреди сборки ядра"
    assert fleet.applied == [("v3.5.0", True)]


def test_the_updater_alive_past_the_grace_is_still_waited_for(fleet, tmp_path):
    _two(fleet)
    fleet.s.fleet_server_act(fleet.tick())
    (tmp_path / "update-running").write_text(f"{os.getpid()}\n", encoding="utf-8")   # pid жив
    fleet.now_shift(fleet_server.SPAWN_GRACE_S + 60)
    assert fleet.tick().server == "" and fleet.run()["state"] == "running"
    assert fleet.run().get("server"), "отметка шага снята, пока апдейтер жив"


def test_an_updater_that_died_without_a_restart_rolls_the_run_back(fleet, tmp_path):
    _two(fleet)
    fleet.s.fleet_server_act(fleet.tick())
    (tmp_path / "update-running").write_text("999999\n", encoding="utf-8")         # pid мёртв
    fleet.now_shift(fleet_server.SPAWN_GRACE_S + 5)
    fleet.tick()
    run = fleet.run()
    assert run["state"] in ("rolling_back", "rolled_back") and run["failed_on"] == "main"
    assert run["steps"] == [] and "не применилось" in run["reason"]
    assert fleet.applied == [("v3.5.0", True)], "ступень повторена"


def test_twenty_minutes_without_an_end_is_a_failed_step(fleet, tmp_path):
    _two(fleet)
    fleet.s.fleet_server_act(fleet.tick())
    (tmp_path / "update-running").write_text(f"{os.getpid()}\n", encoding="utf-8")
    fleet.now_shift(fleet_server.STEP_MAX_S + 1)
    fleet.tick()
    assert fleet.run()["failed_on"] == "main" and "20 минут" in fleet.run()["reason"]


def test_the_fact_is_a_new_process_with_the_expected_version(fleet):
    _two(fleet)
    fleet.s.fleet_server_act(fleet.tick())
    fleet.server("3.4.0")                                 # новый процесс — прежняя версия (апдейтер откатил)
    fleet.tick()
    assert fleet.run()["failed_on"] == "main", "сервер на прежней версии признан обновлённым"


# ── logic-1: такт по расписанию — только по делу ─────────────────────────────

def test_the_scheduled_tick_runs_only_when_there_is_something_to_do(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    assert fleet.s.fleet_needs_tick() is False, "такт без дела"
    fleet.s.fleet_slot_set(1, {"id": "a1b2c3d4", "state": "done", "hold": True, "run": "x"})
    assert fleet.s.fleet_needs_tick() is True, "удержание без такта не снимется"
    fleet.s.fleet_slot_set(1, {"id": "a1b2c3d4", "state": "failed", "next_at": "2020-01-01T00:00:00+00:00"})
    assert fleet.s.fleet_needs_tick() is True, "повтор в срок без такта"
    fleet.s.fleet_slot_set(1, {})
    assert fleet.start() == "" and fleet.s.fleet_needs_tick() is True, "идущий ход без такта"


async def test_the_fleet_job_skips_without_work(fleet, monkeypatch):
    from awgbot.runtime.linkserver import upd as fleet_upd
    ticks = []

    async def tick(services):
        ticks.append(1)
    monkeypatch.setattr(fleet_upd, "fleet_run_tick", tick)
    await fleet_upd.fleet_job(fleet.s)
    assert ticks == []
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.start()
    await fleet_upd.fleet_job(fleet.s)
    assert ticks == [1]


# ── logic-3: шлюз на связи без ответа ────────────────────────────────────────

def test_a_gateway_online_without_an_answer_for_half_an_hour_fails_the_step(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.start()
    assert [s for s, _ in fleet.sent(fleet.tick())] == [1]
    fleet.ack(1, "accepted")
    fleet.now_shift(29 * 60)
    assert fleet.tick().send == [] and fleet.run()["state"] == "running"
    fleet.now_shift(31 * 60)
    fleet.tick()
    run = fleet.run()
    assert run["state"] in ("rolling_back", "rolled_back") and run["failed_on"] == 1
    assert run["reason"] == "шлюз на связи, но не ответил за 30 минут"


# ── logic-4, logic-5: откат ступеней сервера ─────────────────────────────────

def _chain(fleet):
    """Сервер прошёл две ступени: v3.4.1 (прежняя в удержании), затем v3.5.0."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", main=BOTH), rel("v3.5.0", main={"requires": "3.4.1"}))
    fleet.slot(1)
    fleet.start()
    out = fleet.tick()
    assert out.step.tag == "v3.4.1"
    out = fleet.finish_server(out, "3.4.1")
    assert out.server == "apply" and out.step.tag == "v3.5.0"
    fleet.s.fleet_server_act(out)


def test_a_guard_rollback_drops_only_the_last_server_step(fleet, tmp_path):
    import json
    _chain(fleet)
    (tmp_path / "update-rolled-back").write_text(json.dumps({"version": "3.5.0", "prev": "3.4.1"}), encoding="utf-8")
    fleet.server("3.4.1")                                 # сторож вернул одну ступень
    out = fleet.s.fleet_on_start()
    run = fleet.run()
    assert run["steps"] == [["main", "v3.4.1"]], "сняты все ступени сервера, хотя сторож вернул одну"
    assert out.server == "revert", "ступень v3.4.1 не откатывается — набор ни на прежней, ни на цели"
    fleet.s.fleet_server_act(out)
    assert ["__update_revert"] in fleet.spawned
    fleet.server("3.4.0")
    fleet.tick()
    assert fleet.run()["state"] == "rolled_back"


def test_interleaved_server_steps_are_undone_by_one_server_revert(fleet):
    """[ступень сервера, шлюз, ступень сервера] (needs_gw второй ступени):
    удержание сервера одно — откат сервера один, сразу на версию до хода,
    шлюз между ними — своим upd_revert; второго отката сервера нет."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    fleet.start()
    run = fleet.run()
    run.update(steps=[["main", "v3.4.1"], ["gw", 1, "v3.5.0"], ["main", "v3.5.0"]], undo=3)
    fleet.s.fleet_run_set(run)
    fleet.s.fleet_slot_set(1, {"id": "a1b2c3d4", "run": run["id"], "target": "v3.5.0", "prev": "v3.4.0",
                               "state": "done", "hold": True})
    fleet.server("3.5.0")
    out = fleet.s._fleet_rollback_begin(fleet.run(), _fc.fleet_order.FleetOut(), 1, "сеть")
    assert out.server == "revert" and fleet.run()["server"]["want"] == "v3.4.0"
    fleet.s.fleet_server_act(out)
    fleet.server("3.4.0")
    out = fleet.tick()
    assert fleet.sent(out, "upd_revert") == [(1, {"id": "a1b2c3d4", "to": "v3.4.0"})]
    out = fleet.ack(1, "reverted")
    assert out.server == "" and fleet.run()["state"] == "rolled_back", "второй откат сервера"
    assert fleet.spawned.count(["__update_revert"]) == 1


# ── logic-7: команды вне хода и ход ──────────────────────────────────────────

def test_a_catch_up_in_flight_holds_the_run_and_an_unstarted_one_yields(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    fleet.s.fleet_slot_set(1, {"id": "c1c2c3c4", "run": "", "target": "v3.4.1", "state": "accepted",
                               "at": _fc.timeutil.now_iso(), "why": "catchup"})
    fleet.start()
    assert fleet.tick().send == [], "ход пошёл, пока у шлюза идёт догон"
    fleet.online(1, "3.4.1")                              # догон довёл: новый агент на связи, затем ответ
    out = fleet.ack_id(1, "c1c2c3c4", "done", "v3.4.1")
    assert [s for s, _ in fleet.sent(out)] == [2], "ход не пошёл после итога догона"
    # не начатый догон («sent») посреди хода по hello не повторяется — ход даст свою команду
    fleet.s.fleet_slot_set(1, {"id": "d1d2d3d4", "run": "", "target": "v3.4.1", "state": "sent"})
    out = fleet.hello(1, "3.4.1")
    assert not [b for s, b in fleet.sent(out) if b["id"] == "d1d2d3d4"], "догон повторён посреди хода"
    assert fleet.st(1)["state"] == ""


# ── logic-8, logic-13: удержание и вернувшийся ───────────────────────────────

def test_the_hold_of_a_superseded_run_is_committed_by_its_command_id(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1, "3.4.1")
    fleet.s.fleet_run_set({"id": "newrun01", "tag": "v3.4.1", "state": "done", "steps": []})
    fleet.s.fleet_slot_set(1, {"id": "a1b2c3d4", "run": "oldrun01", "target": "v3.4.1", "state": "done",
                               "hold": True})
    out = fleet.tick()
    assert fleet.sent(out, "upd_commit") == [(1, {"id": "a1b2c3d4"})], "удержание снято молча"
    assert fleet.st(1)["hold"] is False


def test_a_gateway_back_mid_step_is_done_only_by_its_answer(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    fleet.active(1)
    fleet.start()
    first = fleet.sent(fleet.tick())[0][0]
    fleet.ack(first, "accepted")
    fleet.offline(first)
    fleet.tick()
    fleet.now_shift(16 * 60)
    fleet.tick()
    assert fleet.st(first)["state"] == "lost"
    out = fleet.hello(first, "3.4.1")                     # вернулся уже новой версией, ответ ещё не пришёл
    assert fleet.st(first)["state"] == "accepted", "«done» выведено из версии в hello"
    assert not fleet.sent(out), "вернувшемуся посреди шага — новая команда или повтор"
    out = fleet.ack(first, "done", step="v3.4.1")
    assert ["gw", first, "v3.4.1"] in fleet.run()["steps"]


def test_a_gateway_lost_before_it_took_the_command_gets_it_again_by_the_same_id(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.start()
    sent = fleet.sent(fleet.tick())
    rid = sent[0][1]["id"]
    fleet.offline(1)
    fleet.tick()
    fleet.now_shift(16 * 60)
    fleet.tick()
    out = fleet.hello(1, "3.4.0")
    assert fleet.sent(out) == [(1, {**sent[0][1]})] and fleet.sent(out)[0][1]["id"] == rid


# ── вне оси: ручное обновление сервера — следование ─────────────────────────

def test_a_server_updated_by_hand_is_followed_not_caught_up(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", both=True))
    fleet.slot(1, "3.4.0")
    fleet.server("3.4.1")                                 # `awg-bot update` руками, хода не было
    sent = fleet.sent(fleet.tick())
    assert sent and sent[0][1]["why"] == "follow"
    out = fleet.gw_done(1, "3.4.1")
    assert [n.text for n in out.notes] == ["✅ «Шлюз 1» обновлён до v3.4.1 вслед за сервером AWG"]
