"""Всплывашка после переключения VPN-транзита: «применится по каналу» — только
агенту, который документ конфигурации принимает; устаревшему агенту —
«обнови агента шлюза», иначе обещание не сбудется."""
from __future__ import annotations

import pytest

from awgbot.bot.callbacks import GwSlotCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config, settings
from awgbot.util import gwlink
from tests.conftest import FakeCallback, FakeMessage

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


@pytest.fixture()
def slot(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
    monkeypatch.setattr(services, "gateway_resolver_addr", lambda g: "10.9.1.1" if g.lan_mode else "")
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "get_bool",
                        lambda k, d=False: True if k == "app.routing.enabled" else d)
    monkeypatch.setattr(services, "routing_status", lambda: (True, "ок"))
    return 1


@pytest.mark.parametrize("version, caps, tail", [
    ("3.3.1", ("cfg1",), "применится автоматически по каналу конфигурации"),
    ("3.2.1", (), "агент шлюза устарел и конфигурацию по каналу не принимает — обнови агента шлюза"),
])
async def test_the_transit_toast_names_what_will_actually_happen(services, slot, fake_bot, version, caps, tail):
    services.gwlink_session_opened(slot, version, gwlink.PROTO, caps=caps)
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=ADMIN, bot=fake_bot)
    await sh.gw_slot_lan_yes(cb, GwSlotCB(action="lan_yes", slot=slot, val="1"), services)
    assert services.db.gateway(slot).lan_mode == 1
    assert (f"VPN-транзит включён: {tail}", True) in cb.answers, cb.answers
