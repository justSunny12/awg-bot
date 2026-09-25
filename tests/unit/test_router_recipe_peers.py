"""Рецепт роутера при доступе между подсетями за шлюзами: сам роутер отвечает со своего адреса по основной таблице,
мимо заворота на шлюз, — без маршрута до подсетей других шлюзов его ответ
уходит провайдеру, и роутер из соседней сети недостижим. Рецепт добавляет эти
маршруты и абзац-объяснение; без соседей текст прежний. Подсети соседей агент
берёт из юнита обвязки (PEER_HOME_NETS)."""
from __future__ import annotations

import pytest

from awgbot.bot import texts
from awgbot.bot.texts.routing import ROUTER_IP_PLACEHOLDER
from awgbot.domain.gateway import GatewayServices
from awgbot.infra import gwguard
from awgbot.infra.db import Database

NET = "192.168.1.0/24"
GW = "192.168.1.2"
PEERS = ["192.168.68.0/24", "10.20.0.0/16"]
NOTE = "<b>Доступ между подсетями</b>"


def _mikrotik(text: str) -> list[str]:
    return text.split("<b>MikroTik RouterOS 7</b>\n<pre>", 1)[1].split("</pre>", 1)[0].splitlines()


def _openwrt(text: str) -> list[str]:
    return text.split("<b>OpenWrt</b>\n<pre>", 1)[1].split("</pre>", 1)[0].splitlines()


# ── текст рецепта ────────────────────────────────────────────────────────────

def test_recipe_without_peers_is_unchanged():
    """Доступ между подсетями выключен — ни маршрутов до соседей, ни абзаца:
    лишняя строка `/ip route add` с чужой подсетью у человека без второго
    шлюза увела бы трафик в никуда."""
    base = texts.gateway_router_text("«NASPi»", NET, GW)
    for empty in (None, [], [""]):
        assert texts.gateway_router_text("«NASPi»", NET, GW, peer_nets=empty) == base, empty
    assert NOTE not in base
    assert [ln for ln in _mikrotik(base) if ln.startswith("/ip route add")] == \
        [f"/ip route add dst-address=0.0.0.0/0 gateway={GW} routing-table=antiblock"], _mikrotik(base)
    assert [ln for ln in _openwrt(base) if ln.startswith("ip route add")] == \
        [f"ip route add default via {GW} table vpn"], _openwrt(base)


def test_recipe_with_peers_adds_routes_and_the_note_and_nothing_else():
    """Маршрут до каждой соседней подсети — в обоих рецептах, через адрес
    шлюза; абзац в конце. Всё остальное — слово в слово как без соседей:
    убрать добавленное — получится прежний текст."""
    base = texts.gateway_router_text("«NASPi»", NET, GW)
    got = texts.gateway_router_text("«NASPi»", NET, GW, peer_nets=PEERS)
    mt = [f"/ip route add dst-address={p} gateway={GW}" for p in PEERS]
    ow = [f"ip route add {p} via {GW}" for p in PEERS]
    assert [ln for ln in _mikrotik(got) if ln in mt] == mt, _mikrotik(got)
    assert [ln for ln in _openwrt(got) if ln in ow] == ow, _openwrt(got)
    head, sep, note = got.partition("\n\n" + NOTE)
    assert sep, "абзаца «Доступ между подсетями» нет"
    assert all(f"<code>{p}</code>" in note for p in PEERS), note
    stripped = "\n".join(ln for ln in head.split("\n") if ln not in mt and ln not in ow)
    assert stripped == base, "кроме маршрутов до соседей и абзаца рецепт изменился"


def test_peer_routes_stand_before_dns_and_dhcp_lines():
    """Маршруты — до раздачи DNS/DHCP, последней строкой которой рецепт
    кончается: человек копирует блок целиком, и порядок внутри <pre> —
    порядок команд. В MikroTik — после правила asym-via-gw."""
    got = texts.gateway_router_text("«NASPi»", NET, GW, peer_nets=PEERS)
    mt = _mikrotik(got)
    first = mt.index(f"/ip route add dst-address={PEERS[0]} gateway={GW}")
    assert mt[first - 1].startswith("add action=accept chain=forward comment=asym-via-gw"), mt
    assert mt[first + len(PEERS)] == f"/ip dhcp-server network set [find] dns-server={GW}", mt
    ow = _openwrt(got)
    first = ow.index(f"ip route add {PEERS[0]} via {GW}")
    assert ow[first - 1] == "uci set firewall.@defaults[0].flow_offloading='0'", ow
    assert ow[first + len(PEERS)] == f"uci add_list dhcp.lan.dhcp_option='6,{GW}'", ow


def test_peer_routes_use_the_placeholder_when_the_gateway_address_is_unknown():
    """Основной бот адреса шлюза в подсети не знает — маршрут до соседей
    через тот же плейсхолдер, что и остальной рецепт, а не через пустоту."""
    got = texts.gateway_router_text("«NASPi»", NET, "", peer_nets=PEERS[:1])
    assert f"/ip route add dst-address={PEERS[0]} gateway={ROUTER_IP_PLACEHOLDER}" in _mikrotik(got), got
    assert f"ip route add {PEERS[0]} via {ROUTER_IP_PLACEHOLDER}" in _openwrt(got), got
    assert "gateway=\n" not in got and "via \n" not in got


def test_peer_nets_are_escaped():
    """Подсети приезжают из юнита и базы — `<` без экранирования Telegram
    отвергает, и рецепт не пришёл бы вовсе."""
    got = texts.gateway_router_text("«NASPi»", NET, GW, peer_nets=["10.0.0.0/8<b>&"])
    assert "10.0.0.0/8&lt;b&gt;&amp;" in got and "10.0.0.0/8<b>" not in got, got
    assert got.count("10.0.0.0/8&lt;b&gt;&amp;") == 3, "маршрут MikroTik, маршрут OpenWrt, абзац"


# ── lan_router_params у агента ───────────────────────────────────────────────

@pytest.fixture()
def svc(tmp_path):
    db = Database(tmp_path / "gw.db"); db.init_schema()
    return GatewayServices(db)


def _unit(monkeypatch, tmp_path, body: str):
    """Настоящий юнит обвязки во временном каталоге — чтение идёт через
    gwguard.unit_env, как на малине."""
    unit = tmp_path / "awg-link-gw.service"
    unit.write_text("[Service]\n" + body, encoding="utf-8")
    monkeypatch.setattr(gwguard.config, "GW_UNIT", unit.name)
    real = gwguard.Path
    monkeypatch.setattr(gwguard, "Path", lambda p: real(str(p).replace("/etc/systemd/system", str(tmp_path))))


def test_lan_router_params_carry_peer_nets_from_the_unit(svc, tmp_path, monkeypatch):
    """Строки юнита — в том виде, в каком их пишет routing-gw-setup.sh
    (значение в кавычках, подсети через пробел)."""
    _unit(monkeypatch, tmp_path, 'Environment="HOME_SUBNETS=192.168.1.0/24 10.0.0.0/24"\n'
                                 'Environment="PEER_HOME_NETS=192.168.68.0/24 10.20.0.0/16"\n')
    monkeypatch.setattr(gwguard, "script_status", lambda: {"LAN_ADDR": GW})
    assert svc.lan_router_params() == (NET, GW, PEERS)


def test_lan_router_params_without_peers(svc, tmp_path, monkeypatch):
    """Доступ выключен (строка пустая) или юнит старого образца (строки нет) —
    пустой список, рецепт без маршрутов до соседей."""
    monkeypatch.setattr(gwguard, "script_status", lambda: {"LAN_ADDR": GW})
    for body in ('Environment="HOME_SUBNETS=192.168.1.0/24"\nEnvironment="PEER_HOME_NETS="\n',
                 'Environment="HOME_SUBNETS=192.168.1.0/24"\n'):
        _unit(monkeypatch, tmp_path, body)
        assert svc.lan_router_params() == (NET, GW, []), body


def test_lan_router_params_without_the_unit(svc, monkeypatch):
    """Обвязки нет (откатили) — пусто во всех трёх, без исключения."""
    monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
    monkeypatch.setattr(gwguard, "script_status", lambda: {})
    assert svc.lan_router_params() == ("", "", [])
