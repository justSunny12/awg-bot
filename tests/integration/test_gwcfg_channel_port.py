"""
Смена порта управляющего канала без обрыва: прежний порт сервер слушает и
открывает в файерволе ещё неделю (gwchannel.cfg: gwcfg_channel_port_tick,
gwcfg_channel_ports; nftguard: link_channel_extra).

Шлюзы узнают новый порт из документа конфигурации и переезжают по одному;
закрой сервер старый порт сразу — те, кто ещё не получил документ, стучались
бы в пустоту, и доставка самого документа стала бы невозможна.
"""
from __future__ import annotations

import datetime

import pytest

from awgbot.core import settings
from awgbot.infra import nftguard
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def _port(monkeypatch, port: int) -> None:
    real = settings.get_int
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: port if k == "app.routing.link_channel_port" else real(k, d))


def test_the_previous_port_stays_for_a_week_after_a_change(services, monkeypatch):
    _port(monkeypatch, 8787)
    services.gwcfg_channel_port_tick()
    assert services.gwcfg_channel_ports() == [8787], "без смены — один порт"
    _port(monkeypatch, 9099)
    services.gwcfg_channel_port_tick()
    assert services.gwcfg_channel_ports() == [9099, 8787], "прежний порт пропал сразу после смены"
    services.gwcfg_channel_port_tick()
    assert services.gwcfg_channel_ports() == [9099, 8787], "повторный такт сдвинул память о смене"
    old = timeutil.to_iso(timeutil.now() - datetime.timedelta(days=8))
    services.db.set_state(services._CHPORT_PREV_KEY, f"8787|{old}")
    assert services.gwcfg_channel_ports() == [9099], "прежний порт пережил неделю"
    assert services.db.get_state(services._CHPORT_PREV_KEY) == "", "отслуживший порт не забыт"


def test_a_change_back_to_the_old_port_does_not_keep_it_twice(services, monkeypatch):
    _port(monkeypatch, 8787)
    services.gwcfg_channel_port_tick()
    _port(monkeypatch, 9099)
    services.gwcfg_channel_port_tick()
    _port(monkeypatch, 8787)
    services.gwcfg_channel_port_tick()
    assert services.gwcfg_channel_ports() == [8787, 9099]


def test_the_firewall_opens_the_previous_port_too(monkeypatch):
    """Правило входа канала — с двумя портами, пока прежний не отслужил;
    совпадающий с текущим или нулевой «прежний» в правило не попадает."""
    monkeypatch.setattr(nftguard, "link_ifaces", lambda: ["awglink"])
    monkeypatch.setattr(nftguard, "link_peer_addrs", lambda: ["10.99.99.2"])
    monkeypatch.setattr(nftguard, "link_channel_port", lambda: 9099)
    spec = nftguard.GuardSpec(ssh_port=22, link_peers4=["10.99.99.2"], link_ifs=["awglink"],
                              link_channel_port=9099, link_channel_extra=[8787])
    text = nftguard.render(spec)
    assert "tcp dport { 9099, 8787 } accept" in text
    spec.link_channel_extra = []
    assert "tcp dport 9099 accept" in nftguard.render(spec)
    monkeypatch.setattr(nftguard, "tunnel_nets", lambda: [])
    monkeypatch.setattr(nftguard, "listen_ports", lambda: [])
    monkeypatch.setattr(nftguard, "_tunnel_ifs", lambda: [])
    monkeypatch.setattr(nftguard, "_docker_bridge_subnets", lambda: [])
    monkeypatch.setattr(nftguard, "_resolve", lambda host: [])
    built = nftguard.build_spec([], channel_extra=[9099, 0, 8787])
    assert built.link_channel_extra in ([8787], []), "совпадающий с текущим и нулевой порт отсеяны"
