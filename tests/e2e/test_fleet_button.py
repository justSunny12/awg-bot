"""«⬆️ Обновить до vX» основного бота — набор: диалог с порядком,
«⬅️ Отмена», «⬆️ Обновить» — ход и одно живое сообщение без звука,
итог с кнопками, «🔁 Повторить»; отказы — всплывашкой: ход уже
идёт, цель сменилась, GitHub не ответил (хода нет).
"""
from __future__ import annotations

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import UpdateCB
from awgbot.bot.handlers import fleet as fleet_flow
from awgbot.bot.texts import fleet as ft
from awgbot.core import config
from awgbot.infra import updates
from awgbot.runtime.linkserver import upd as ls_upd
from tests.conftest import FakeCallback, FakeMessage
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.e2e
RELEASES = [rel("v3.4.0", both=True), rel("v3.5.0", both=True)]


@pytest.fixture()
def world(fleet, monkeypatch, fake_bot):
    fleet.releases(*RELEASES)
    fleet.slot(1)
    monkeypatch.setattr(updates, "list_releases", lambda **_: list(RELEASES))
    shown = []

    async def on_fleet(services, out):
        await fleet_flow.fleet_show(fake_bot, services, out)
        shown.append(out)
    monkeypatch.setattr(ls_upd, "_on_fleet", on_fleet)
    return fleet


def _cb(fake_bot, data: str = "") -> FakeCallback:
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=fake_bot)
    return FakeCallback(data=data, message=msg, user_id=config.ADMIN_ID, bot=fake_bot)


async def test_the_button_opens_the_dialog_with_the_order(world, fake_bot):
    cb = _cb(fake_bot)
    await fleet_flow.install(cb, world.s, "v3.5.0")
    kind, text, markup = cb.message.sent[-1]
    assert kind == "edit_text" and text == ft.fleet_confirm(world.s.fleet_plan(world.s.fleet_target()))
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["⬅️ Отмена", "⬆️ Обновить"]
    assert markup.inline_keyboard[0][1].callback_data == UpdateCB(action="go", tag="v3.5.0").pack()
    assert world.run() == {}, "диалог начал ход"


async def test_go_starts_the_fleet_with_one_silent_live_message(world, fake_bot):
    cb = _cb(fake_bot)
    await fleet_flow.start(cb, world.s, "v3.5.0")
    run = world.run()
    assert run["state"] == "running" and run["msg"] and run["steps"] == [["main", "v3.5.0"]]
    sends = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sends) == 1 and sends[0][2].startswith("⏳ <b>Обновление до v3.5.0</b>")
    assert world.applied == [("v3.5.0", True)], "ступень сервера не ушла апдейтеру"
    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edits and "🟡 Сервер AWG — обновляется" in edits[-1][2]


async def test_refusals_are_toasts_and_start_nothing(world, fake_bot, monkeypatch):
    cb = _cb(fake_bot)
    await fleet_flow.start(cb, world.s, "v3.9.9")
    assert cb.answers[-1] == (texts.UPDATE_TARGET_CHANGED_TOAST, True) and world.run() == {}

    def boom(**_):
        raise updates.UpdateError("GitHub не ответил")
    monkeypatch.setattr(updates, "list_releases", boom)
    await fleet_flow.start(cb, world.s, "v3.5.0")
    assert cb.answers[-1] == (texts.UPDATE_CHECK_FAILED_TOAST, True) and world.run() == {}
    monkeypatch.setattr(updates, "list_releases", lambda **_: list(RELEASES))
    await fleet_flow.start(cb, world.s, "v3.5.0")
    await fleet_flow.install(cb, world.s, "v3.5.0")
    assert cb.answers[-1] == ("Обновление уже идёт", True)


async def test_the_final_message_keeps_the_history_and_offers_the_menu(world, fake_bot):
    cb = _cb(fake_bot)
    await fleet_flow.start(cb, world.s, "v3.5.0")
    world.server("3.5.0")                          # апдейтер ушёл с первым тактом — новый процесс
    out = world.tick()
    world.gw_done(1, "3.5.0")
    await fleet_flow.fleet_show(fake_bot, world.s, None)
    run = world.run()
    assert run["state"] == "done"
    assert (run["msg"][0], run["msg"][1]) in [tuple(x) for x in world.s.db.get_state_json("update_report_msgs", [])]
    assert out is not None


async def test_retry_after_a_rollback_starts_again(world, fake_bot):
    cb = _cb(fake_bot)
    await fleet_flow.start(cb, world.s, "v3.5.0")
    assert world.s.fleet_tick().server == "", "второй такт снова позвал апдейтер сервера"
    world.server("3.5.0")
    world.tick()
    out = world.ack(1, "failed", error="сеть")
    world.s.fleet_server_act(out)
    world.server("3.4.0")
    world.s.fleet_tick()
    assert world.run()["state"] == "rolled_back"
    retry = _cb(fake_bot)
    await fleet_flow.retry(retry, world.s)
    assert world.run()["state"] == "running" and world.run()["steps"] == [["main", "v3.5.0"]]
    assert retry.answers[0][0] == texts.UPDATE_STARTING
