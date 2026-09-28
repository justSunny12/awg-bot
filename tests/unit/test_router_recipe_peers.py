"""Рецепт роутера при связанных подсетях: сам роутер отвечает со своего адреса по основной таблице,
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
NOTE = "Связь подсетей: сам роутер"


def _mikrotik(text: str) -> list[str]:
    return text.split("<b>MikroTik RouterOS 7</b>\n<pre>", 1)[1].split("</pre>", 1)[0].splitlines()


def _openwrt(text: str) -> list[str]:
    return text.split("<b>OpenWrt</b>\n<pre>", 1)[1].split("</pre>", 1)[0].splitlines()


def _recipe(tab: str, gw: str = GW, peer_nets=None) -> str:
    return texts.gateway_router_text("NASPi", NET, gw, peer_nets=peer_nets, tab=tab)


# ── текст рецепта ────────────────────────────────────────────────────────────

def test_recipe_without_peers_is_unchanged():
    """Связь подсетей выключена — ни маршрутов до соседей, ни абзаца:
    лишняя строка `/ip route add` с чужой подсетью у человека без второго
    шлюза увела бы трафик в никуда. Так на обеих вкладках."""
    for tab in ("mt", "ow"):
        base = _recipe(tab)
        for empty in (None, [], [""]):
            assert _recipe(tab, peer_nets=empty) == base, (tab, empty)
        assert NOTE not in base, tab
    assert [ln for ln in _mikrotik(_recipe("mt")) if ln.startswith("/ip route add")] == \
        [f"/ip route add dst-address=0.0.0.0/0 gateway={GW} routing-table=antiblock"], _recipe("mt")
    assert [ln for ln in _openwrt(_recipe("ow")) if ln.startswith("ip route add")] == \
        [f"ip route add default via {GW} table vpn"], _recipe("ow")


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
    assert got.startswith(f"❓ Роутер для NASPi · {NET} · шлюз {ROUTER_IP_PLACEHOLDER}\n"), got
    assert "панели" not in got, got


def test_recipe_with_peers_adds_routes_and_the_note_and_nothing_else():
    """Маршрут до каждой соседней подсети — на своей вкладке, через адрес
    шлюза; абзац в конце. Всё остальное — слово в слово как без соседей:
    убрать добавленное — получится прежний текст."""
    mt = [f"/ip route add dst-address={p} gateway={GW}" for p in PEERS]
    ow = [f"ip route add {p} via {GW}" for p in PEERS]
    for tab, lines, parse in (("mt", mt, _mikrotik), ("ow", ow, _openwrt)):
        base = _recipe(tab)
        got = _recipe(tab, peer_nets=PEERS)
        assert [ln for ln in parse(got) if ln in lines] == lines, parse(got)
        head, sep, note = got.partition("\n" + NOTE)
        assert sep, "абзаца «Связь подсетей» нет"
        assert all(f"<code>{p}</code>" in note for p in PEERS), note
        stripped = "\n".join(ln for ln in head.split("\n") if ln not in lines)
        assert stripped == base, f"{tab}: кроме маршрутов до соседей и абзаца рецепт изменился"


def test_peer_routes_stand_before_dns_and_dhcp_lines():
    """Маршруты — до раздачи DNS/DHCP, последней строкой которой рецепт
    кончается: человек копирует блок целиком, и порядок внутри <pre> —
    порядок команд. В MikroTik — после правила asym-via-gw."""
    mt = _mikrotik(_recipe("mt", peer_nets=PEERS))
    first = mt.index(f"/ip route add dst-address={PEERS[0]} gateway={GW}")
    assert mt[first - 1].startswith("add action=accept chain=forward comment=asym-via-gw"), mt
    assert mt[first + len(PEERS)] == f"/ip dhcp-server network set [find] dns-server={GW}", mt
    ow = _openwrt(_recipe("ow", peer_nets=PEERS))
    first = ow.index(f"ip route add {PEERS[0]} via {GW}")
    assert ow[first - 1] == "uci set firewall.@defaults[0].flow_offloading='0'", ow
    assert ow[first + len(PEERS)] == f"uci add_list dhcp.lan.dhcp_option='6,{GW}'", ow


def test_peer_routes_use_the_placeholder_when_the_gateway_address_is_unknown():
    """Основной бот адреса шлюза в подсети не знает — маршрут до соседей
    через тот же плейсхолдер, что и остальной рецепт, а не через пустоту."""
    mt = _recipe("mt", gw="", peer_nets=PEERS[:1])
    ow = _recipe("ow", gw="", peer_nets=PEERS[:1])
    assert f"/ip route add dst-address={PEERS[0]} gateway={ROUTER_IP_PLACEHOLDER}" in _mikrotik(mt), mt
    assert f"ip route add {PEERS[0]} via {ROUTER_IP_PLACEHOLDER}" in _openwrt(ow), ow
    assert "gateway=\n" not in mt and "via \n" not in ow


def test_peer_nets_are_escaped():
    """Подсети приезжают из юнита и базы — `<` без экранирования Telegram
    отвергает, и рецепт не пришёл бы вовсе."""
    for tab in ("mt", "ow"):
        got = _recipe(tab, peer_nets=["10.0.0.0/8<b>&"])
        assert "10.0.0.0/8&lt;b&gt;&amp;" in got and "10.0.0.0/8<b>" not in got, got
        assert got.count("10.0.0.0/8&lt;b&gt;&amp;") == 2, f"{tab}: маршрут и абзац"


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
