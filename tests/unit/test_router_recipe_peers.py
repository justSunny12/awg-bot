"""Рецепт роутера и связь подсетей: о соседях рецепт не говорит ничего. Устройства
сети отвечают соседям заворотом на шлюз (правило «всё нелокальное — на шлюз»
покрывает и адреса в туннеле), а сам роутер — шлюзу: тот маскарадит к его
адресу трафик подсетей других шлюзов. Строк маршрутов до соседей и абзаца о
самом роутере нет ни у основного бота, ни у агента, сколько бы соседей ни было.

Цена ошибки: лишняя строка `/ip route add` с префиксом соседа в туннеле на
роутере увела бы ответы мимо маскарада — и человек копирует блок целиком."""
from __future__ import annotations

import types

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import GwCB, GwSlotCB
from awgbot.bot.texts.routing import ROUTER_IP_PLACEHOLDER
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database
from tests.conftest import FakeCallback, FakeMessage

NET = "192.168.1.0/24"
GW = "192.168.1.2"
PEERS = ["10.241.0.0/16", "10.242.0.0/16"]


def _mikrotik(text: str) -> list[str]:
    return text.split("<b>MikroTik RouterOS 7</b>\n<pre>", 1)[1].split("</pre>", 1)[0].splitlines()


def _openwrt(text: str) -> list[str]:
    return text.split("<b>OpenWrt</b>\n<pre>", 1)[1].split("</pre>", 1)[0].splitlines()


def _recipe(tab: str, gw: str = GW) -> str:
    return texts.gateway_router_text("NASPi", NET, gw, tab=tab)


def _no_peers(text: str) -> None:
    """Ни маршрута до соседей, ни абзаца о самом роутере, ни префиксов в туннеле."""
    assert "Связь подсетей" not in text and "сам роутер" not in text, text
    assert not any(p in text for p in PEERS) and "10.24" not in text, text


# ── текст рецепта ────────────────────────────────────────────────────────────

def test_the_recipe_has_no_neighbour_routes_on_either_tab():
    """На обеих вкладках единственный маршрут — по умолчанию через шлюз в
    своей таблице; маршрутов до подсетей других шлюзов нет."""
    mt, ow = _recipe("mt"), _recipe("ow")
    assert [ln for ln in _mikrotik(mt) if ln.startswith("/ip route add")] == \
        [f"/ip route add dst-address=0.0.0.0/0 gateway={GW} routing-table=antiblock"], mt
    assert [ln for ln in _openwrt(ow) if ln.startswith("ip route add")] == \
        [f"ip route add default via {GW} table vpn"], ow
    _no_peers(mt)
    _no_peers(ow)


def test_the_recipe_does_not_take_neighbour_subnets_at_all():
    """Параметра подсетей соседей у рецепта больше нет: передать их нечем, и
    ни один вызывающий не вернёт строки по ошибке."""
    with pytest.raises(TypeError):
        texts.gateway_router_text("NASPi", NET, GW, peer_nets=PEERS)          # type: ignore[call-arg]


def test_the_recipe_ends_with_the_tab_hint():
    """Абзаца после рецепта нет: последняя строка — подсказка своей вкладки."""
    assert _recipe("mt").endswith("Правило asym-via-gw — выше drop invalid; fasttrack выключить")
    assert _recipe("ow").endswith("iptables — в /etc/firewall.user")


def test_a_tab_shows_only_its_own_recipe():
    """Вкладки: на MikroTik нет команд OpenWrt и наоборот — человек копирует
    блок целиком, и чужие команды в нём сломали бы роутер."""
    mt, ow = _recipe("mt"), _recipe("ow")
    assert "<b>MikroTik RouterOS 7</b>" in mt and "<b>OpenWrt</b>" not in mt and "uci commit" not in mt
    assert "<b>OpenWrt</b>" in ow and "<b>MikroTik RouterOS 7</b>" not in ow and "/ip firewall mangle" not in ow
    assert texts.gateway_router_text("NASPi", NET, GW) == mt, "вкладка по умолчанию — MikroTik"


def test_recipe_head_names_the_gateway_net_and_address_and_drops_the_panel_hint():
    """Шапка — имя, подсеть и адрес шлюза одной строкой; строки «адрес шлюза —
    в панели бота шлюза» больше нет (основной бот показывает плейсхолдер)."""
    got = _recipe("mt", gw="")
    assert got.startswith(f"❓ <b>Роутер для NASPi</b> · <code>{NET}</code> · шлюз <code>{ROUTER_IP_PLACEHOLDER}</code>\n"), got
    assert "панели" not in got, got


# ── основной бот: «❓ Роутер» в карточке слота ───────────────────────────────

class _MainServices:
    """Слот с VPN-транзитом и соседями: подсети соседей в состоянии экрана
    есть — рецепт их не берёт."""

    def gateway_screen_state(self, slot, lazy_ping=True):
        gw = types.SimpleNamespace(id=slot, lan_subnets=[NET], label="", lan_mode=True)
        return {"gateway": gw, "device": types.SimpleNamespace(name="Pi4"), "peer_nets": list(PEERS)}


@pytest.mark.parametrize("tab", ["mt", "ow"])
async def test_the_main_bot_recipe_has_no_neighbour_lines(fake_bot, tab):
    from awgbot.bot.handlers.settings import slots
    msg = FakeMessage(chat_id=1, user_id=1, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=1, bot=fake_bot)
    await slots.gw_slot_router(cb, GwSlotCB(action="router", slot=1, val=tab), _MainServices())
    text = msg.sent[-1][1]
    assert text.startswith("❓ <b>Роутер для Pi4</b>"), text
    _no_peers(text)
    assert text == texts.gateway_router_text("Pi4", NET, tab=tab)


# ── агент: «❓ Роутер» на экране «🔀 VPN-транзит» ────────────────────────────

@pytest.fixture()
def svc(tmp_path):
    db = Database(tmp_path / "gw.db"); db.init_schema()
    yield GatewayServices(db)
    db.close()


def _unit(monkeypatch, tmp_path, body: str):
    """Настоящий юнит обвязки во временном каталоге — чтение идёт через
    gwguard.unit_env, как на шлюзе."""
    unit = tmp_path / "awg-link-gw.service"
    unit.write_text("[Service]\n" + body, encoding="utf-8")
    monkeypatch.setattr(gwguard.config, "GW_UNIT", unit.name)
    real = gwguard.Path
    monkeypatch.setattr(gwguard, "Path", lambda p: real(str(p).replace("/etc/systemd/system", str(tmp_path))))


def test_lan_router_params_do_not_carry_neighbours(svc, tmp_path, monkeypatch):
    """Подсеть — первая из юнита, адрес — из статуса скрипта; PEER_HOME_NETS
    в юните есть, но в рецепт не идёт."""
    _unit(monkeypatch, tmp_path, 'Environment="HOME_SUBNETS=192.168.1.0/24 10.0.0.0/24"\n'
                                 f'Environment="PEER_HOME_NETS={" ".join(PEERS)}"\n')
    monkeypatch.setattr(gwguard, "script_status", lambda: {"LAN_ADDR": GW})
    assert svc.lan_router_params() == (NET, GW)


def test_lan_router_params_without_the_unit(svc, monkeypatch):
    """Обвязки нет (откатили) — пусто в обоих, без исключения."""
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
    monkeypatch.setattr(gwguard, "script_status", lambda: {})
    assert svc.lan_router_params() == ("", "")


@pytest.mark.parametrize("tab", ["mt", "ow"])
async def test_the_agent_recipe_has_no_neighbour_lines(svc, fake_bot, tmp_path, monkeypatch, tab):
    from awgbot.bot.handlers import gateway as gh
    from awgbot.core import config
    _unit(monkeypatch, tmp_path, 'Environment="HOME_SUBNETS=192.168.1.0/24"\n'
                                 f'Environment="PEER_HOME_NETS={" ".join(PEERS)}"\n')
    monkeypatch.setattr(gwguard, "script_status", lambda: {"LAN_ADDR": GW})
    svc.set_link_role(True, standby=True, name="NASPi")
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=config.ADMIN_ID, bot=fake_bot)
    await gh.gw_transit_router(cb, GwCB(action="lan_router", val=tab), svc)
    text = msg.sent[-1][1]
    _no_peers(text)
    assert text == texts.gateway_router_text("NASPi", NET, GW, tab=tab)
