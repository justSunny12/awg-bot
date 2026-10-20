"""Собственный адрес сервера `network.self_host` (domain/services/selfhost.py)
и где он действует: Endpoint в документе слота (канал конфигурации),
`ENDPOINT_HOST` окружения скрипта линка, хост токена подключения шлюза.

С парой серверов адрес для клиентов (`server_host`) переезжает на резерв
по A-записи, а линки своих шлюзов должны смотреть на сам сервер. Пока
`self_host` пуст, всё обязано остаться как в прошлой версии.

Цена ошибки: пустой `self_host`, поменявший хоть байт документа, — после
обновления каждый шлюз получает новое поколение документа и перезапускает
линк и обвязку без причины; заданный, но не дошедший до Endpoint, — в
переезд линки шлюзов уходят вслед за клиентами на резерв; кривой
`self_host`, попавший в конфиг или в окружение скрипта, — `awg-quick` на
шлюзе не поднимает линк, а в окружении — подстановка в shell.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from awgbot.core import config, settings
from awgbot.domain.services import selfhost
from awgbot.domain.services.gwjoin.issue import GwJoinIssueMixin
from awgbot.domain.services.types import ServiceError
from tests.integration import test_gwcfg_delivery as _delivery

pytestmark = pytest.mark.integration

# хост ВПС с источником конфига линка и слот с VPN-транзитом — как в тестах документа слота
host = _delivery.host
slot = _delivery.slot
_parsed = _delivery._parsed


@pytest.fixture()
def live(monkeypatch):
    """Настройки словарём поверх настоящих (как «✏️ Домен» без рестарта)."""
    conf: dict = {}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None: conf[k] if k in conf else real(k, d))
    monkeypatch.setattr(config, "SERVER_HOST", "")
    return conf


# ── сам адрес ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("self_host, server_host, want", [
    ("", "vpn.example.net", "vpn.example.net"),
    ("198.51.100.7", "vpn.example.net", "198.51.100.7"),
    ("srv-a.example.net", "vpn.example.net", "srv-a.example.net"),
    ("  srv-a.example.net ", "vpn.example.net", "srv-a.example.net"),
    ("srv a; reboot", "vpn.example.net", "vpn.example.net"),        # кривой — игнорируется
    ("$(reboot)", "vpn.example.net", "vpn.example.net"),
    ("", "bad host;rm", ""),
    ("", "", ""),
])
def test_own_host_prefers_self_host_and_ignores_a_crooked_one(live, self_host, server_host, want):
    live["app.network.self_host"] = self_host
    live["app.network.server_host"] = server_host
    assert selfhost.own_host() == want


# ── документ слота ───────────────────────────────────────────────────────────

def test_an_empty_self_host_leaves_the_slot_document_as_it_was(services, slot, host):
    """Обновление до версии с ключом: шаблон досеял `self_host: ""` — отпечаток
    документа тот же, поколение не растёт, шлюзам доставлять нечего."""
    host.conf["app.network.server_host"] = "vpn.example.net"
    host.conf.pop("app.network.self_host", None)
    before = services.gwcfg_doc(slot)
    host.conf["app.network.self_host"] = ""
    after = services.gwcfg_doc(slot)
    assert after["hash"] == before["hash"] and after["gen"] == before["gen"], "пустой self_host изменил документ"
    assert after["sections"] == before["sections"]
    assert _parsed(after["sections"]["link"]["conf"]).get("Peer", "Endpoint") == "vpn.example.net:51821"


def test_a_self_host_moves_only_the_endpoint_of_the_link(services, slot, host):
    host.conf["app.network.server_host"] = "vpn.example.net"
    before = services.gwcfg_doc(slot)
    host.conf["app.network.self_host"] = "srv-a.example.net"
    after = services.gwcfg_doc(slot)
    assert after["gen"] == before["gen"] + 1, "смена адреса линка — новое поколение документа"
    old = before["sections"]["link"]["conf"].splitlines()
    new = after["sections"]["link"]["conf"].splitlines()
    diff = [(a, b) for a, b in zip(old, new) if a != b]
    assert len(old) == len(new) and diff == [("Endpoint = vpn.example.net:51821", "Endpoint = srv-a.example.net:51821")], diff
    for name in ("env", "uplink", "mail", "token", "server"):
        assert after["sections"][name] == before["sections"][name], f"раздел {name} изменился от self_host"


def test_a_crooked_self_host_does_not_reach_the_document(services, slot, host):
    host.conf["app.network.server_host"] = "vpn.example.net"
    host.conf["app.network.self_host"] = "srv-a.example.net\nPostUp = reboot"
    text = services.gwcfg_link_conf(slot, services.gwcfg_env(slot))
    assert _parsed(text).get("Peer", "Endpoint") == "vpn.example.net:51821"
    assert "PostUp" not in text and "reboot" not in text


def test_with_no_valid_address_at_all_the_source_endpoint_stays(services, slot, host):
    host.conf["app.network.server_host"] = ""
    host.conf["app.network.self_host"] = "bad host"
    text = services.gwcfg_link_conf(slot, services.gwcfg_env(slot))
    assert _parsed(text).get("Peer", "Endpoint") == "vps.example.org:51821"


# ── окружение скрипта линка ──────────────────────────────────────────────────

_GW = SimpleNamespace(link_if="awglink", link_port=443, link_cidr="10.99.99.0/30")


def test_the_link_script_gets_self_host_as_endpoint_host(services, live):
    live["app.network.server_host"] = "vpn.example.net"
    live["app.network.self_host"] = ""
    assert services._slot_env(_GW)["ENDPOINT_HOST"] == "vpn.example.net", "пустой self_host — как раньше"
    live["app.network.self_host"] = "198.51.100.7"
    assert services._slot_env(_GW)["ENDPOINT_HOST"] == "198.51.100.7"
    live["app.network.self_host"] = "x;reboot"
    assert services._slot_env(_GW)["ENDPOINT_HOST"] == "vpn.example.net", "кривой self_host ушёл в окружение"
    live["app.network.server_host"] = ""
    assert "ENDPOINT_HOST" not in services._slot_env(_GW)


# ── токен подключения шлюза ──────────────────────────────────────────────────

def test_the_join_token_host_is_self_host_when_set(live):
    live["app.network.server_host"] = "vpn.example.net"
    live["app.network.self_host"] = ""
    assert GwJoinIssueMixin._gwjoin_host() == "vpn.example.net"
    live["app.network.self_host"] = "srv-a.example.net"
    assert GwJoinIssueMixin._gwjoin_host() == "srv-a.example.net"
    live["app.network.self_host"] = "srv a"
    assert GwJoinIssueMixin._gwjoin_host() == "vpn.example.net"


def test_without_any_address_the_join_token_is_refused(live):
    live["app.network.server_host"] = ""
    live["app.network.self_host"] = ""
    with pytest.raises(ServiceError, match="server_host"):
        GwJoinIssueMixin._gwjoin_host()
