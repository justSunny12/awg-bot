"""Экраны «❓ Настройка роутера» при доступе между подсетями за шлюзами: у
основного бота (карточка слота) подсети соседей берутся из состояния слота,
у агента — из юнита обвязки вместе с адресом шлюза. В обоих рецепт получает
маршруты до соседей только тогда, когда доступ реально включён."""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.bot.callbacks import GwCB, GwSlotCB
from awgbot.bot.handlers import gateway as gh
from awgbot.bot.handlers import settings as sh
from awgbot.bot.texts.routing import ROUTER_IP_PLACEHOLDER
from awgbot.domain.gateway import GatewayServices
from awgbot.infra.db import Database
from tests.conftest import FakeCallback, FakeMessage
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_slots_ui import _acb, _peer_conf, _screen, _slot1, _slot2

pytestmark = pytest.mark.e2e
slots = _slots_ui.slots
NOTE = "<b>Доступ между подсетями</b>"


# ── основной бот: карточка слота ─────────────────────────────────────────────

def _two_lan_slots(services, slots):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.gateway_set_home_subnets(1, "192.168.1.0/24")
    services.gateway_set_home_subnets(2, "192.168.68.0/24")
    services.db.gateway_update(1, lan_mode=1); services.db.gateway_update(2, lan_mode=1)


async def _router(services, fake_bot, slot):
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_router(cb, GwSlotCB(action="router", slot=slot), services)
    return _screen(nav)


async def test_slot_router_recipe_routes_to_the_other_gateways_lan(services, slots, fake_bot, monkeypatch):
    """Доступ между подсетями включён: рецепт слота 1 ведёт роутер к подсети
    слота 2 через шлюз (адрес — плейсхолдер: основной бот его не знает), и
    наоборот; своей подсети в маршрутах до соседей нет."""
    store = _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    store["app.routing.peer_nets.enabled"] = True
    text, labels = await _router(services, fake_bot, 1)
    assert f"/ip route add dst-address=192.168.68.0/24 gateway={ROUTER_IP_PLACEHOLDER}" in text, text
    assert f"ip route add 192.168.68.0/24 via {ROUTER_IP_PLACEHOLDER}" in text, text
    assert NOTE in text and "dst-address=192.168.1.0/24 gateway=" not in text
    assert labels == ["⬅️ Назад"], labels
    text2, _ = await _router(services, fake_bot, 2)
    assert f"/ip route add dst-address=192.168.1.0/24 gateway={ROUTER_IP_PLACEHOLDER}" in text2, text2
    assert "dst-address=192.168.68.0/24 gateway=" not in text2


async def test_slot_router_recipe_without_peer_access_has_no_peer_routes(services, slots, fake_bot, monkeypatch):
    """Тумблер выключен — рецепт без маршрутов до соседей и без абзаца, хотя
    у соседа подсеть есть: маршрут в сеть, куда доступ закрыт, человеку не
    нужен."""
    _peer_conf(monkeypatch)
    _two_lan_slots(services, slots)
    text, _ = await _router(services, fake_bot, 1)
    assert NOTE not in text and "192.168.68.0/24" not in text, text


# ── агент: экран «Локальная сеть» → «Настройка роутера» ─────────────────────

@pytest.fixture()
def gw_svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    return GatewayServices(d)


async def _lan_router(gw_svc, fake_bot, monkeypatch, params):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "naspi")
    monkeypatch.setattr(gw_svc, "lan_router_params", lambda: params)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await gh.gw_lan_router(cb, gw_svc)
    kind, text, markup = msg.sent[-1]
    assert kind == "edit_text", msg.sent
    return text, markup


async def test_agent_router_recipe_routes_to_peers_via_its_own_address(gw_svc, fake_bot, monkeypatch):
    """Агент знает свой адрес в подсети: маршрут до соседей — через него, не
    через плейсхолдер; «Назад» — на экран локальной сети."""
    text, markup = await _lan_router(gw_svc, fake_bot, monkeypatch,
                                     ("192.168.1.0/24", "192.168.1.2", ["192.168.68.0/24", "10.20.0.0/16"]))
    assert "Настройка роутера: naspi" in text
    for p in ("192.168.68.0/24", "10.20.0.0/16"):
        assert f"/ip route add dst-address={p} gateway=192.168.1.2" in text, text
        assert f"ip route add {p} via 192.168.1.2" in text, text
    assert NOTE in text and ROUTER_IP_PLACEHOLDER not in text
    back = markup.inline_keyboard[-1][0]
    assert back.callback_data == GwCB(action="lan").pack(), back.callback_data


async def test_agent_router_recipe_without_peers(gw_svc, fake_bot, monkeypatch):
    text, _ = await _lan_router(gw_svc, fake_bot, monkeypatch, ("192.168.1.0/24", "192.168.1.2", []))
    assert NOTE not in text, "абзац про доступ между подсетями без соседей"
    assert [ln for ln in text.splitlines() if ln.startswith("/ip route add")] == \
        ["/ip route add dst-address=0.0.0.0/0 gateway=192.168.1.2 routing-table=antiblock"], text
