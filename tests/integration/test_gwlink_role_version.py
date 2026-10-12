"""
Версия сервера в `role` канала и пересчёт цели обновления по событиям канала:
сервер называет свою версию в каждом `role` (агент 3.2.x лишнее поле
пропускает), агент её хранит; шлюз пришёл с новой версией агента — сервер
пересчитывает цель по сохранённому списку релизов без GitHub и уведомляет
крючком main один раз на тег.

Сокеты настоящие (петля), сервер — боевой LinkServer поверх временной БД;
фикстуры слушателя и шлюза — общие с проверками фидов и роли.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.core import config
from awgbot.infra import updates
from awgbot.runtime import linkserver
from awgbot.util import gwlink, timeutil
from tests.integration import test_gwlink_lists_role_diag as _role
from tests.integration.test_gwlink_channel import _Gw, _until

pytestmark = pytest.mark.integration

# фикстуры теста фидов и роли: слушатель канала со слотом NASPi и шлюз на той стороне
link, pi = _role.link, _role.pi
KEY, _hello, _next, _up = _role.KEY, _role._hello, _role._next, _role._up


async def test_the_role_carries_the_server_version_and_the_agent_keeps_it(services, link, pi, monkeypatch):
    """Версию сервер говорит в каждом `role`: по ней агент выбирает цель
    обновления (#gw_needs_main_X)."""
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.0")
    await _up(services, pi)
    await _until(lambda: pi["agent"].server_version())
    assert pi["agent"].server_version() == "3.3.0"


async def test_an_old_agent_skips_the_server_version_in_the_role(services, link):
    """Агент 3.2.x берёт из `role` только active, standby и name — лишнее
    поле его не ломает (dispatch того образца — в заглушке)."""
    host, port = link._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port, limit=8 * gwlink.MAX_LINE), key=KEY)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.2.1"})
    role = await _next(gw, "role")
    assert role is not None and role["server"] == config.INSTALLED_VERSION

    class _Agent321:
        def set_link_role(self, active, standby=None, name=None):
            self.got = (active, standby, name)

    old = _Agent321()
    old.set_link_role(bool(role.get("active")), role.get("standby"), role.get("name"))   # v3.2.1 linkclient
    assert old.got == (True, False, "NASPi")


async def test_a_hello_with_a_new_agent_version_opens_the_server_update(services, link, monkeypatch):
    """Шлюз пришёл с новой версией агента — сервер пересчитывает цель по
    сохранённому списку (без GitHub) и уведомляет крючком main."""
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.0")
    rel = [updates.Release("v3.3.0", (3, 3, 0), "#requires_main_3.2.0", None, None),
           updates.Release("v3.3.1", (3, 3, 1), "#requires_main_3.3.0\n#main_needs_gw_3.3.0", None, None)]
    monkeypatch.setattr(updates, "list_releases", lambda: rel)
    services.gwlink_snapshot_in(1, {"agent_version": "3.2.1"}, 1, True)
    services.db.set_state("gwlink_seen_1", timeutil.now_iso())
    assert services.update_scan() is None, "шлюз на связи с агентом 3.2.1 — v3.3.1 ждёт"

    def boom():
        raise AssertionError("пересчёт по событию пошёл на GitHub")
    monkeypatch.setattr(updates, "list_releases", boom)
    got = []

    async def on_update(nxt):
        got.append(nxt.tag)
    monkeypatch.setattr(linkserver, "_on_update", on_update)
    gw = await _hello(link)                                   # агент 3.4.0
    await _until(lambda: got)
    assert got == ["v3.3.1"] and services.update_available_tag() == "v3.3.1"
    await gw.send("snap", {"rev": 1, "agent_version": "3.4.0"})
    await asyncio.sleep(0.2)
    assert got == ["v3.3.1"], "уведомление — один раз на тег"
