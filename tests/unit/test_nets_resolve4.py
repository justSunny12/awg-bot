"""Разрешение имени в IPv4 с потолком (util/nets.resolve4) и проверка
IP-литерала (is_ip / is_ipv4) — одно место вместо копий: перерезолв аплинка
у агента шлюза (domain/gateway/reserve/uplink.resolve), адрес основы для
DNAT хвоста у резерва (inbound_back._relay_host_ip), ADMIN_IPS и RESOLVER
документа конфигурации шлюза (domain/gwcfg/check), сверка ADMIN_IPS снимка
(gateway_link/drift).

Цена ошибки: getaddrinfo своего таймаута не знает, а socket.setdefaulttimeout
его не ограничивает и меняет умолчание всем сокетам процесса — параллельный
поток (опрос Telegram, канал пары) на время резолва получал чужой таймаут, а
при молчащем резолвере перерезолв аплинка висел без предела и держал задание
планировщика агента; у резерва повисший резолв имени основы держал такт
возврата (released) сверх правила «наружу ≤ 10 с». Первый адрес не по
порядку ответа — хвост и аплинк не на тот адрес, что отдаёт запись; IPv6 там,
где ждут IPv4, — правило DNAT или Endpoint, которые не встают.
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from awgbot.domain.gateway.reserve import uplink
from awgbot.domain.services.reserve import inbound_back
from awgbot.util import nets

pytestmark = pytest.mark.unit

_relay_ip = inbound_back.ReserveInboundBackMixin._relay_host_ip


def _answer(*addrs):
    return lambda *a, **k: [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", (ip, 0)) for ip in addrs]


@pytest.fixture()
def hung(monkeypatch):
    """Повисший системный резолвер: отвечает через 5 с; `seen` — умолчание
    таймаута сокетов, которое видел вызов."""
    release = threading.Event()
    seen: list = []

    def gai(*a, **kw):
        seen.append(socket.getdefaulttimeout())
        release.wait(5)
        return [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("203.0.113.9", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", gai)
    yield seen
    release.set()


@pytest.mark.resolve_pool
@pytest.mark.parametrize("call", [lambda: nets.resolve4("vpn.example.net", 0.3),
                                  lambda: uplink.resolve("vpn.example.net", 0.3)],
                         ids=["nets", "uplink"])
def test_a_hung_resolver_is_cut_at_the_timeout_and_the_default_stays(hung, call):
    before = socket.getdefaulttimeout()
    t0 = time.monotonic()
    assert call() == ""
    took = time.monotonic() - t0
    assert took < 2, f"ожидание {took:.1f} с вместо потолка"
    assert hung == [before] and socket.getdefaulttimeout() == before, "умолчание таймаута процесса тронуто"


@pytest.mark.resolve_pool
def test_the_relay_address_of_a_hung_name_is_cut_at_the_ceiling(hung, monkeypatch):
    monkeypatch.setattr(nets, "RESOLVE_TIMEOUT", 0.3)
    before = socket.getdefaulttimeout()
    t0 = time.monotonic()
    assert _relay_ip("vpn.example.net") == ""
    assert time.monotonic() - t0 < 2, "резолв адреса основы для хвоста без предела"
    assert socket.getdefaulttimeout() == before


def test_the_resolver_ceiling_stays_within_the_outbound_rule():
    assert 0 < nets.RESOLVE_TIMEOUT <= 10 and 0 < uplink.RESOLVE_TIMEOUT <= 10


def test_the_first_ipv4_in_resolver_order_is_taken(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _answer("203.0.113.10", "203.0.113.9", "203.0.113.10"))
    assert nets.resolve4("vpn.example.net") == "203.0.113.10"
    assert uplink.resolve("vpn.example.net") == "203.0.113.10"
    assert _relay_ip("vpn.example.net") == "203.0.113.10"


def test_only_ipv4_is_asked_and_a_stray_ipv6_is_skipped(monkeypatch):
    asked = []

    def gai(host, port, family=0, *a, **kw):
        asked.append(family)
        return [(socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("2001:db8::1", 0, 0, 0)),
                (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("203.0.113.9", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", gai)
    assert nets.resolve4("vpn.example.net") == "203.0.113.9"
    assert asked == [socket.AF_INET]


@pytest.mark.parametrize("err", [socket.gaierror(-2, "Name or service not known"), OSError("down"),
                                 UnicodeError("label too long")])
def test_a_failed_lookup_gives_nothing(err, monkeypatch):
    def fail(*a, **k):
        raise err
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert nets.resolve4("vpn.example.net") == ""
    assert uplink.resolve("vpn.example.net") == ""
    assert _relay_ip("vpn.example.net") == ""


def test_an_empty_answer_gives_nothing(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _answer())
    assert nets.resolve4("vpn.example.net") == ""


def test_an_ipv6_literal_is_not_a_relay_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("IP-литерал резолвится"))
    assert _relay_ip("2001:db8::1") == ""
    assert _relay_ip("203.0.113.9") == "203.0.113.9"


@pytest.mark.parametrize("tok, ip, ip4", [("203.0.113.9", True, True), ("2001:db8::1", True, False),
                                          ("vpn.example.net", False, False), ("", False, False),
                                          ("203.0.113.9/32", False, False), ("010.0.0.1", False, False)])
def test_ip_literals(tok, ip, ip4):
    assert nets.is_ip(tok) is ip and uplink.is_ip(tok) is ip
    assert nets.is_ipv4(tok) is ip4


def test_the_gateway_document_takes_only_ipv4_admins_and_resolver():
    from awgbot.domain.gwcfg import check
    from awgbot.domain.gwcfg.doc import CfgError
    env = {"CLIENT_SUBNET": "10.8.1.0/24", "ADMIN_IPS": "10.8.1.2 10.8.1.7", "LAN_MODE": "1",
           "LAN_SUBNETS": "192.168.1.0/24", "RESOLVER": "192.168.1.1", "PEER_LAN_NETS": "",
           "LINK_CHANNEL_PORT": "8787"}
    assert check.check_env(env)["ADMIN_IPS"] == "10.8.1.2 10.8.1.7"
    for key, bad in (("ADMIN_IPS", "10.8.1.2 2001:db8::1"), ("RESOLVER", "2001:db8::1"),
                     ("RESOLVER", "dns.example.net")):
        with pytest.raises(CfgError, match=key):
            check.check_env({**env, key: bad})
