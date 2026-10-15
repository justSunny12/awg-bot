"""Сверка версий ролей на живой БД: основной бот держит релиз с
`roles.main.needs_gw` = X, пока шлюз, бывший на связи за 2 дня, ниже X;
молчащий дольше — не держит, о нём предупреждение. Агент держит релиз с
`roles.gw.needs_main` = X, пока не знает сервер AWG не ниже X. Изменились версии
другой стороны — цель пересчитывается по сохранённому списку, без GitHub, и
о новой допустимой — одно уведомление на тег.
"""
from __future__ import annotations

import datetime

import pytest

from awgbot.core import config
from awgbot.domain.gateway import GatewayServices
from awgbot.domain.selfupdate import UPDATE_PEER_FRESH_DAYS
from awgbot.infra import updates
from awgbot.infra.db import Database
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def _r(tag: str, roles: dict, gen: int = 1, body: str = "") -> updates.Release:
    """Релиз с метаданными (roles — как в awg-bot-release.json)."""
    meta = {"schema": 1, "version": tag.lstrip("v"), "awg_gen": gen, "roles": roles}
    return updates.Release(tag=tag, version=updates.parse_version(tag), body=body,
                           asset_url=f"https://api/assets/{tag}", sha256="a" * 64,
                           meta=updates.meta.check(meta, tag))


MAIN_RELEASES = [
    _r("v3.3.0", {"main": {"requires": "3.2.0"}, "gw": {"requires": "3.2.0"}}),
    _r("v3.3.1", {"main": {"requires": "3.3.0", "needs_gw": "3.3.0"}}, body="- сервер без шимов"),
]

GW_RELEASES = [
    _r("v3.3.0", {"main": {"requires": "3.2.0"}, "gw": {"requires": "3.2.0"}}),
    _r("v3.3.1", {"main": {"requires": "3.3.0"}}),
    _r("v3.3.2", {"gw": {"requires": "3.3.0", "needs_main": "3.3.1"}}, body="- агент без шимов"),
]


def _github(monkeypatch, releases):
    """Список релизов «с GitHub»; calls — сколько раз за ним ходили."""
    calls = []
    monkeypatch.setattr(updates, "list_releases", lambda **_: calls.append(1) or list(releases))
    return calls


def _no_github(monkeypatch):
    def boom(**_):
        raise AssertionError("пересчёт пошёл на GitHub — должен идти по сохранённому списку")
    monkeypatch.setattr(updates, "list_releases", boom)


def _ago(days: float) -> str:
    return timeutil.to_iso(timeutil.now() - datetime.timedelta(days=days))


# ── основной бот ─────────────────────────────────────────────────────────────

@pytest.fixture()
def vps(services, make_active_client, monkeypatch):
    """Сервер 3.3.0 и один слот «Pi5»; канал ещё не открывался."""
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.0")
    monkeypatch.setattr(config, "ROLE", "client")
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    pi = services.add_device(admin.id, "Pi5")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    return services


def test_a_gateway_on_line_with_an_old_agent_holds_the_release(vps, monkeypatch):
    _github(monkeypatch, MAIN_RELEASES)
    vps.gwlink_session_opened(1, "3.2.1", 2, [])
    assert vps.update_scan() is None, "шлюз на связи с агентом 3.2.1 — v3.3.1 ему не по силам"
    assert vps.update_available_tag() == ""
    w = vps.update_waiting()
    assert w["tag"] == "v3.3.1" and w["need"] == "3.3.0" and w["role"] == "client"
    assert [(p.slot, p.name, p.version) for p in w["peers"]] == [(1, "Pi5", "3.2.1")]


def test_a_gateway_silent_for_three_days_does_not_hold_but_is_warned_about(vps, monkeypatch):
    _github(monkeypatch, MAIN_RELEASES)
    vps.gwlink_session_opened(1, "3.2.1", 2, [])
    vps.gwlink_snapshot_in(1, {"agent_version": "3.2.1"}, 1, True)
    vps.gwlink_session_closed(1)                               # сессионная строка стёрта — версия из снимка
    vps.db.set_state("gwlink_seen_1", _ago(UPDATE_PEER_FRESH_DAYS + 1))
    found = vps.update_scan()
    assert found is not None and found.tag == "v3.3.1", "молчащий дольше 2 дней сервер не держит"
    assert vps.update_waiting() is None
    silent = vps.update_silent_peers(found)
    assert silent["tag"] == "v3.3.1" and silent["need"] == "3.3.0"
    assert [(p.name, p.version, p.fresh) for p in silent["peers"]] == [("Pi5", "3.2.1", False)]


def test_a_gateway_seen_yesterday_holds_even_without_a_session(vps, monkeypatch):
    """Сессии нет (шлюз отвалился час назад), версия — из снимка: держит."""
    _github(monkeypatch, MAIN_RELEASES)
    vps.gwlink_snapshot_in(1, {"agent_version": "3.2.1"}, 1, True)
    vps.db.set_state("gwlink_seen_1", _ago(1))
    assert vps.update_scan() is None
    assert vps.update_waiting()["peers"][0].version == "3.2.1"


def test_the_version_comes_from_the_snapshot_when_there_is_no_session(vps, monkeypatch):
    _github(monkeypatch, MAIN_RELEASES)
    vps.gwlink_snapshot_in(1, {"agent_version": "3.3.0"}, 1, True)
    vps.db.set_state("gwlink_seen_1", _ago(0.5))
    assert vps.update_scan().tag == "v3.3.1"
    assert vps.update_silent_peers(vps.update_next()) is None, "шлюз догнал — предупреждать не о ком"


def test_a_slot_without_a_device_is_a_warning_not_a_block(vps, monkeypatch):
    _github(monkeypatch, MAIN_RELEASES)
    vps.gwlink_session_opened(1, "3.2.1", 2, [])
    real = vps.db.get_device
    monkeypatch.setattr(vps.db, "get_device", lambda i: None if i == vps.db.gateway(1).device_id else real(i))
    found = vps.update_scan()
    assert found is not None and found.tag == "v3.3.1"
    silent = vps.update_silent_peers(found)
    assert [p.name for p in silent["peers"]] == ["шлюз 1"]


def test_a_gateway_never_seen_is_a_warning_with_an_unknown_version(vps, monkeypatch):
    _github(monkeypatch, MAIN_RELEASES)
    found = vps.update_scan()
    assert found.tag == "v3.3.1"
    silent = vps.update_silent_peers(found)
    assert [(p.version, p.seen) for p in silent["peers"]] == [("", "")]


def test_regate_on_a_new_agent_version_notifies_once_without_github(vps, monkeypatch):
    _github(monkeypatch, MAIN_RELEASES)
    vps.gwlink_session_opened(1, "3.2.1", 2, [])
    assert vps.update_scan() is None
    _no_github(monkeypatch)
    # шлюз обновился: новая сессия с новой версией
    vps.gwlink_session_closed(1)
    vps.gwlink_session_opened(1, "3.3.0", 2, [])
    nxt = vps.update_regate()
    assert nxt is not None and nxt.tag == "v3.3.1", "о новой допустимой цели — уведомление"
    assert "сервер без шимов" in nxt.body, "тело релиза — из сохранённого списка"
    assert vps.update_available_tag() == "v3.3.1" and vps.update_waiting() is None
    assert vps.update_regate() is None, "уведомление — один раз на тег"


def test_regate_on_a_snapshot_with_a_new_agent_version(vps, monkeypatch):
    _github(monkeypatch, MAIN_RELEASES)
    vps.gwlink_snapshot_in(1, {"agent_version": "3.2.1"}, 1, True)
    vps.db.set_state("gwlink_seen_1", _ago(0.1))
    assert vps.update_scan() is None
    _no_github(monkeypatch)
    vps.gwlink_snapshot_in(1, {"agent_version": "3.3.0"}, 1, True)
    assert vps.update_regate().tag == "v3.3.1"
    assert vps.update_regate() is None


def test_regate_without_a_saved_list_does_nothing(vps, monkeypatch):
    _no_github(monkeypatch)
    assert vps.update_regate() is None
    assert vps.update_available_tag() == ""


def test_regate_after_an_update_ignores_the_list_of_the_previous_version(vps, monkeypatch):
    _github(monkeypatch, MAIN_RELEASES)
    vps.update_scan()
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.1")
    _no_github(monkeypatch)
    assert vps.update_regate() is None


def test_regate_respects_muted_notifications(vps, monkeypatch):
    _github(monkeypatch, MAIN_RELEASES)
    vps.gwlink_session_opened(1, "3.2.1", 2, [])
    vps.update_scan()
    vps.mute_updates()
    _no_github(monkeypatch)
    vps.gwlink_session_opened(1, "3.3.0", 2, [])
    assert vps.update_regate() is None, "уведомления выключены — молчим"
    assert vps.update_available_tag() == "v3.3.1", "а строка «⬆️ Доступна» — есть"


def test_without_conditions_nothing_changes(vps, monkeypatch):
    """Релизы без строк условий — выбор как до сверки, ожидания нет."""
    _github(monkeypatch, [_r("v3.3.0", {"main": {"requires": "3.2.0"}}),
                          _r("v3.3.1", {"main": {"requires": "3.3.0"}})])
    vps.gwlink_session_opened(1, "3.2.1", 2, [])
    assert vps.update_scan().tag == "v3.3.1"
    assert vps.update_waiting() is None and vps.update_silent_peers(vps.update_next()) is None


# ── агент ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.0")
    monkeypatch.setattr(config, "ROLE", "gateway")
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    yield GatewayServices(d)
    d.close()


def test_agent_with_an_unknown_server_waits(agent, monkeypatch):
    """`role` с версией ещё не приходил (канала нет) — релиз с условием ждёт,
    допустимый ниже предлагается."""
    _github(monkeypatch, GW_RELEASES)
    assert agent.server_version() == ""
    assert agent.update_scan() is None, "v3.3.1 агенту не адресован, v3.3.2 ждёт сервер"
    w = agent.update_waiting()
    assert w["tag"] == "v3.3.2" and w["need"] == "3.3.1" and w["role"] == "gateway"
    assert w["peers"][0].version == ""


def test_agent_with_an_old_server_waits(agent, monkeypatch):
    _github(monkeypatch, GW_RELEASES)
    agent.set_link_role(True, False, "Pi5", [], None)        # role без версии — сервер 3.2.x
    assert agent.server_version() == "old"
    assert agent.update_scan() is None
    assert agent.update_waiting()["peers"][0].version == "old"


def test_agent_with_a_known_server_version(agent, monkeypatch):
    _github(monkeypatch, GW_RELEASES)
    agent.set_link_role(True, False, "Pi5", ["cfg1"], "3.3.0")
    assert agent.update_scan() is None and agent.update_waiting()["peers"][0].version == "3.3.0"
    agent.set_link_role(True, False, "Pi5", ["cfg1"], "3.3.1")
    assert agent.update_scan().tag == "v3.3.2"


def test_agent_drops_a_server_version_of_a_foreign_shape(agent):
    for bad in ("3.3", "v3.3.1", "3.3.1-dev", "3.3.1\n#x", 331, ["3.3.1"], "9" * 40):
        agent.set_link_role(True, server=bad)
        assert agent.server_version() == "old", bad
    agent.set_link_role(True, server="3.3.1.2")
    assert agent.server_version() == "3.3.1.2"
    agent.set_link_role(True)                                  # поле не передано — не трогаем
    assert agent.server_version() == "3.3.1.2"


def test_agent_regate_on_a_new_server_version_notifies_once_without_github(agent, monkeypatch):
    _github(monkeypatch, GW_RELEASES)
    agent.set_link_role(True, False, "Pi5", ["cfg1"], "3.3.0")
    assert agent.update_scan() is None
    _no_github(monkeypatch)
    agent.set_link_role(True, False, "Pi5", ["cfg1"], "3.3.1")
    nxt = agent.update_regate()
    assert nxt is not None and nxt.tag == "v3.3.2"
    assert agent.update_available_tag() == "v3.3.2" and agent.update_waiting() is None
    assert agent.update_regate() is None, "уведомление — один раз на тег"


def test_agent_has_no_silent_peers(agent, monkeypatch):
    _github(monkeypatch, GW_RELEASES)
    agent.set_link_role(True, server="3.3.1")
    found = agent.update_scan()
    assert found.tag == "v3.3.2" and agent.update_silent_peers(found) is None


async def test_a_role_with_a_new_server_version_notifies_through_the_hook(agent, monkeypatch):
    """Канал: `role` с новой версией сервера — пересчёт без GitHub и
    уведомление крючком main; повтор того же `role` — тишина."""
    from awgbot.runtime import linkclient
    _github(monkeypatch, GW_RELEASES)
    agent.set_link_role(True, server="3.3.0")
    assert agent.update_scan() is None
    _no_github(monkeypatch)
    got = []

    async def on_update(nxt):
        got.append(nxt.tag)
    monkeypatch.setattr(linkclient._update_hook, "fn", on_update)
    client = linkclient.LinkClient(agent)
    await client._dispatch({"t": "role", "active": True, "caps": ["cfg1"], "server": "3.3.1"})
    assert got == ["v3.3.2"] and agent.server_version() == "3.3.1"
    await client._dispatch({"t": "role", "active": True, "caps": ["cfg1"], "server": "3.3.1"})
    assert got == ["v3.3.2"]
    await client._dispatch({"t": "role", "active": True})            # сервер откатили на 3.2.x
    assert agent.server_version() == "old"
