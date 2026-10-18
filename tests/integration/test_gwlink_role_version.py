"""
Версия сервера в `role` канала: сервер называет свою версию в каждом `role`
(агент 3.2.x — его разбор взят дословно из тега v3.2.1 — лишнее поле
пропускает), агент её хранит — по ней он проверяет `needs_main` команды
набора обновлений.

Сокеты настоящие (петля), сервер — боевой LinkServer поверх временной БД;
фикстуры слушателя и шлюза — общие с проверками фидов и роли.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.core import config
from awgbot.util import gwlink
from tests.integration import test_gwlink_lists_role_diag as _role
from tests.integration.test_gwlink_channel import _Gw, _until

pytestmark = pytest.mark.integration

# фикстуры теста фидов и роли: слушатель канала со слотом NASPi и шлюз на той стороне
link, pi = _role.link, _role.pi
KEY, _hello, _next, _up = _role.KEY, _role._hello, _role._next, _role._up


async def test_the_role_carries_the_server_version_and_the_agent_keeps_it(services, link, pi, monkeypatch):
    """Версию сервер говорит в каждом `role`: по ней агент выбирает цель
    обновления (`roles.gw.needs_main`)."""
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.3.0")
    await _up(services, pi)
    await _until(lambda: pi["agent"].server_version())
    assert pi["agent"].server_version() == "3.3.0"


class _Agent321:
    """Агент v3.2.1 — разбор `role` и его запись дословно из тега v3.2.1
    (runtime/linkclient.py, _dispatch; domain/gateway/report.py,
    set_link_role) поверх словаря вместо БД."""
    _LINK_ROLE_KEY, _LINK_STANDBY_KEY, _LINK_SLOT_NAME_KEY = "gwlink_role", "gwlink_standby", "gwlink_slot_name"

    def __init__(self):
        self.state: dict = {}
        self.db = self
        self.services = self

    def set_state(self, k, v):
        self.state[k] = v

    def set_link_role(self, active: bool, standby=None, name=None) -> None:
        self.db.set_state(self._LINK_ROLE_KEY, "active" if active else "standby")
        if standby is not None:
            self.db.set_state(self._LINK_STANDBY_KEY, "1" if standby else "0")
        if name is not None:
            self.db.set_state(self._LINK_SLOT_NAME_KEY, str(name)[:80])

    async def _dispatch(self, msg: dict) -> None:
        if msg.get("t") == "role":
            await asyncio.to_thread(self.services.set_link_role, bool(msg.get("active")),
                                    msg.get("standby"), msg.get("name"))
            return


async def test_an_old_agent_skips_the_server_version_in_the_role(services, link):
    """Агент 3.2.x берёт из `role` только active, standby и name: настоящее
    сообщение сервера 3.3.0 (с `caps` и `server`) его разбор проходит без
    ошибки и записывает то же, что от сервера 3.2.x."""
    host, port = link._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port, limit=8 * gwlink.MAX_LINE), key=KEY)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.2.1"})
    role = await _next(gw, "role")
    assert role is not None and role["server"] == config.INSTALLED_VERSION and "caps" in role
    assert role["lan_virtual"] == ["10.240.68.0/24"], "и адреса в туннеле — агент 3.2.x их тоже пропускает"
    old = _Agent321()
    await old._dispatch(role)
    assert old.state == {"gwlink_role": "active", "gwlink_standby": "0", "gwlink_slot_name": "NASPi"}


# ── адреса в туннеле сети слота в role ───────────────────────────────────────

async def test_the_role_carries_the_tunnel_addresses_of_the_slot_net(services, link, pi):
    """Сервер называет в `role` адреса в туннеле сети этого слота (двойники
    его подсетей, LanMapMixin.lan_twins); агент хранит их для экрана
    «🔀 VPN-транзит»."""
    assert services.lan_twins(services.db.gateway(1)) == [("192.168.68.0/24", "10.240.68.0/24")]
    await _up(services, pi)
    await _until(lambda: pi["agent"].lan_virtual())
    assert pi["agent"].lan_virtual() == ["10.240.68.0/24"]


async def test_new_subnets_reach_the_agent_with_the_next_role(services, link, pi):
    """`role` сравнивается целиком: подсети слота сменились — адреса в туннеле
    уходят агенту следующим тактом доставки сами."""
    await _up(services, pi)
    await _until(lambda: pi["agent"].lan_virtual())
    services.gateway_set_lan_subnets(1, "192.168.1.0/24 192.168.2.0/24")
    await link.deliver_all()
    await _until(lambda: pi["agent"].lan_virtual() != ["10.240.68.0/24"])
    assert pi["agent"].lan_virtual() == ["10.240.1.0/24", "10.240.2.0/24"]


async def test_a_broken_space_sends_no_tunnel_addresses(services, link, monkeypatch):
    """Служебное пространство неисправно — адресов в туннеле нет ни у кого:
    поле пустое, а не настоящие подсети."""
    monkeypatch.setattr(services, "lan_off", lambda slots=None: ("пересекается с подсетью клиентов", "routing.lan_space"))
    host, port = link._bound[0]
    gw = _Gw(*await asyncio.open_connection(host, port, limit=8 * gwlink.MAX_LINE), key=KEY)
    await gw.send("hello", {"proto": gwlink.PROTO, "agent": "3.4.0"})
    role = await _next(gw, "role")
    assert role is not None and role["lan_virtual"] == [], role


async def test_the_role_warns_once_per_error_and_still_goes(services, link, monkeypatch, caplog):
    """Адреса в туннеле не посчитались — роль уходит без них, а предупреждение
    в журнале одно на текст ошибки, а не каждый такт живости."""
    import logging
    calls = []

    def boom(gw, slots=None):
        calls.append(slots)
        raise RuntimeError("сломалось")
    monkeypatch.setattr(services, "lan_twins", boom)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert link._slot_words(1)["lan_virtual"] == []
    assert sum("адреса в туннеле слота 1" in r.getMessage() for r in caplog.records) == 1
    assert calls and all(s is not None for s in calls), "пространство — по уже загруженным слотам"
