"""Набор обновлений и перезапуск сервера: команды шлюзам — только после их
hello новому процессу (окна ожидания нет, ждём факта); ход и откат
переживают перезапуск (`fleet_target` в базе). Провод — настоящий слушатель
канала на петле: hello шлюза с отставшим агентом → `upd` той же сессией;
`upd_ack` шлюза → итог в базе и следующий шаг.
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.infra import awglock
from awgbot.runtime.linkserver import upd as ls_upd
from awgbot.util import gwlink
from tests.integration import test_gwlink_channel as _ch
from tests.integration.test_gwlink_channel import _connect, _until
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
link = _ch.link
pytestmark = pytest.mark.integration

BOTH = {"requires": "3.4.0"}


def test_after_a_restart_commands_wait_for_the_hello_of_the_new_process(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    fleet.start()
    out = fleet.tick()
    fleet.s.fleet_server_act(out)
    fleet.server("3.5.0")
    fleet.s.gwlink_sessions_reset()                           # старт нового процесса: сокеты прежнего мертвы
    out = fleet.s.fleet_on_start().merge(fleet.tick())
    assert fleet.sent(out) == [], "команда шлюзу по сессии прежнего процесса"
    assert fleet.run()["state"] == "running" and fleet.run()["steps"] == [["main", "v3.5.0"]], "ход не пережил перезапуск"
    out = fleet.hello(1, "3.4.0")
    assert [s for s, _ in fleet.sent(out)] == [1]


def test_a_rollback_survives_the_restart_of_the_server(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    fleet.slot(2)
    fleet.active(1)
    fleet.start()
    out = fleet.finish_server(fleet.tick(), "3.5.0")
    fleet.gw_done(2, "3.5.0")
    out = fleet.ack(1, "failed", error="сеть")
    assert [s for s, _ in fleet.sent(out, "upd_revert")] == [2]
    fleet.s.gwlink_sessions_reset()                           # сервер перезапустился посреди отката
    out = fleet.tick()
    assert fleet.run()["state"] == "rolling_back"
    out = fleet.hello(2, "3.4.0")
    out = fleet.ack(2, "reverted")
    assert out.server == "revert"


async def test_the_wire_carries_the_command_and_the_answer(services, link, monkeypatch, make_active_client):
    """hello шлюза, отставшего от выпуска набора, — upd той же сессией (догон);
    upd_ack done — итог в базе, уведомление крючком main."""
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.1")
    monkeypatch.setattr(awglock, "generation", lambda: 1)
    services._update_store([rel("v3.4.0", both=True), rel("v3.4.1", both=True)], {})
    services.fleet_run_set({"id": "a1b2c3d4", "tag": "v3.4.1", "state": "done", "steps": [], "prev": "v3.4.0"})
    shown: list = []

    async def on_fleet(services_, out):
        shown.extend(n.text for n in out.notes)
    monkeypatch.setattr(ls_upd, "_on_fleet", on_fleet)
    gw = await _connect(link)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.4.0", "caps": ["cfg1", "upd1"]})
    got = None
    for _ in range(10):
        msg = await gw.next_msg()
        if msg is None:
            break
        if msg["t"] == "upd":
            got = msg
            break
    assert got is not None and got["target"] == "v3.4.1" and got["why"] == "catchup"
    await gw.send("upd_ack", {"id": got["id"], "state": "done", "step": "v3.4.1", "version": "3.4.1", "error": ""})
    await _until(lambda: services.fleet_slot(1).get("state") == "done")
    assert services.fleet_slot(1)["state"] == "done"
    await _until(lambda: shown)
    assert shown == ["✅ «NASPi» вышел на связь и обновлён до v3.4.1"]
    await gw.close()


async def test_an_agent_without_upd1_gets_no_command(services, link, monkeypatch):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.1")
    monkeypatch.setattr(awglock, "generation", lambda: 1)
    services._update_store([rel("v3.4.0", both=True), rel("v3.4.1", both=True)], {})
    gw = await _connect(link)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.4.0", "caps": ["cfg1"]})
    kinds = []
    for _ in range(6):
        msg = await gw.next_msg(timeout=0.3)
        if msg is None:
            break
        kinds.append(msg["t"])
    assert "upd" not in kinds, "агенту 3.3.x ушла команда, которой он не знает"
    await gw.close()
