"""
util/lanspace.py — адреса в туннеле для локальных сетей шлюзов: префикс
слота, двойник подсети и адреса, обратный перевод, проверки ввода, разбор
routing.lan_space и проверка служебного пространства; сервисный
lan_space_problem() — входы из конфигурации сервера и БД.

Цена ошибки: двойник не той длины или не того слота — пакет админа уходит
не в ту сеть; пропущенное пересечение двойников — одна из подсетей слота
молча не видна; пространство поверх подсети клиентов или линка — сервер
переводит адреса собственных клиентов.
"""
from __future__ import annotations

import ipaddress
import logging
import re

import pytest

from awgbot.core import config, settings
from awgbot.util import lanspace

pytestmark = pytest.mark.unit

SPACE = "10.240.0.0/12"


@pytest.fixture(autouse=True)
def _fresh_space_cache():
    lanspace._space_from.cache_clear()
    yield
    lanspace._space_from.cache_clear()


# ── эталоны схемы адресов ────────────────────────────────────────────────────

@pytest.mark.parametrize("slot,real,twin", [
    (1, "192.168.1.0/24", "10.240.1.0/24"),
    (2, "192.168.1.0/24", "10.241.1.0/24"),
    (2, "192.168.68.0/22", "10.241.68.0/22"),
    (1, "10.0.0.0/16", "10.240.0.0/16"),
    (1, "172.16.5.0/24", "10.240.5.0/24"),
])
def test_twin_net_reference_table(slot, real, twin):
    assert str(lanspace.twin_net(slot, real)) == twin
    assert str(lanspace.twin_net(slot, real, SPACE)) == twin


def test_twin_addr_keeps_last_two_numbers():
    assert str(lanspace.twin_addr(1, "192.168.1.10")) == "10.240.1.10"
    assert str(lanspace.twin_addr(2, "192.168.69.7")) == "10.241.69.7"
    assert str(lanspace.twin_addr(15, "172.16.5.1")) == "10.254.5.1"


# ── префиксы слотов ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("slot", range(1, 16))
def test_slot_prefixes_1_to_15(slot):
    p = lanspace.slot_prefix(slot)
    assert p == ipaddress.ip_network(f"10.{239 + slot}.0.0/16")
    assert lanspace.slot_of(p) == slot
    assert lanspace.slot_of(f"10.{239 + slot}.7.9") == slot


@pytest.mark.parametrize("slot", [0, 16, 17, -1])
def test_slot_outside_limits_is_refused(slot):
    with pytest.raises(ValueError):
        lanspace.slot_prefix(slot)
    with pytest.raises(ValueError):
        lanspace.twin_net(slot, "192.168.1.0/24")
    with pytest.raises(ValueError):
        lanspace.twin_addr(slot, "192.168.1.1")


def test_max_slots_and_service_net():
    assert lanspace.MAX_SLOTS == 15
    assert str(lanspace.service_net()) == lanspace.SERVICE_NET == "10.255.0.0/16"
    assert str(lanspace.service_net("10.0.0.0/12")) == "10.15.0.0/16"
    # служебная /16 и всё вне пространства — не слот
    assert lanspace.slot_of("10.255.53.53") is None
    assert lanspace.slot_of("10.239.1.1") is None
    assert lanspace.slot_of("192.168.1.1") is None
    assert lanspace.slot_of("10.240.0.0/15") is None      # шире префикса слота
    assert lanspace.slot_of("мусор") is None


def test_other_space_shifts_prefixes():
    assert str(lanspace.slot_prefix(1, "10.0.0.0/12")) == "10.0.0.0/16"
    assert str(lanspace.slot_prefix(3, "172.16.0.0/12")) == "172.18.0.0/16"
    assert str(lanspace.twin_net(2, "192.168.1.0/24", "10.0.0.0/12")) == "10.1.1.0/24"


# ── проверки ввода ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("net,wide", [("10.0.0.0/15", True), ("192.168.0.0/8", True),
                                      ("10.0.0.0/16", False), ("192.168.1.0/24", False),
                                      ("192.168.1.7/32", False)])
def test_wider_than_16_is_refused(net, wide):
    assert lanspace.too_wide(net) is wide
    if wide:
        with pytest.raises(ValueError):
            lanspace.twin_net(1, net)
    else:
        lanspace.twin_net(1, net)


@pytest.mark.parametrize("net,inside", [("10.240.1.0/24", True), ("10.255.53.0/24", True),
                                        ("10.247.0.0/16", True), ("10.0.0.0/8", True),
                                        ("10.239.255.0/24", False), ("192.168.1.0/24", False),
                                        ("10.0.0.0/16", False)])
def test_subnet_in_space(net, inside):
    assert lanspace.in_space(net) is inside
    assert lanspace.in_space(net, SPACE) is inside


def test_twin_clash_exact():
    """Разные подсети, одинаковые две последние цифры — один двойник."""
    assert lanspace.twin_clash(1, "10.10.1.0/24", ["192.168.1.0/24"]) == "192.168.1.0/24"
    assert lanspace.twin_clashes(1, ["192.168.1.0/24", "10.10.1.0/24"]) == [("10.10.1.0/24", "192.168.1.0/24")]


def test_twin_clash_nested_with_different_network_numbers():
    """10.240.0.0/22 накрывает 10.240.2.0/24 — пересечение вложенностью."""
    assert lanspace.twin_clash(1, "10.0.2.0/24", ["192.168.0.0/22"]) == "192.168.0.0/22"
    assert lanspace.twin_clash(1, "192.168.0.0/22", ["10.0.2.0/24"]) == "10.0.2.0/24"
    assert lanspace.twin_clashes(1, ["192.168.0.0/22", "10.0.2.0/24"]) == [("10.0.2.0/24", "192.168.0.0/22")]


def test_twin_neighbours_are_fine():
    """10.240.0.0/23 и 10.240.2.0/24 — соседи, не пересекаются."""
    assert lanspace.twin_clash(1, "10.0.2.0/24", ["192.168.0.0/23"]) == ""
    assert lanspace.twin_clashes(1, ["192.168.0.0/23", "10.0.2.0/24"]) == []


def test_twin_clashes_keep_first_and_skip_junk():
    nets = ["192.168.1.0/24", "10.10.1.0/24", "172.16.1.0/24", "мусор", "10.0.0.0/8", "192.168.2.0/24"]
    assert lanspace.twin_clashes(1, nets) == [("10.10.1.0/24", "192.168.1.0/24"),
                                              ("172.16.1.0/24", "192.168.1.0/24")]
    # первая выпала бы, только если сама с кем-то до неё; отброшенная не мешает следующим
    assert lanspace.twin_clash(1, "192.168.2.0/24", ["10.0.0.0/8", "мусор"]) == ""


def test_whole_16_takes_the_slot():
    assert lanspace.twin_clash(2, "192.168.7.0/24", ["10.0.0.0/16"]) == "10.0.0.0/16"


# ── обратный перевод ─────────────────────────────────────────────────────────

def test_real_addr_back_translation():
    slots = {1: ["192.168.1.0/24"], 2: ["192.168.1.0/24", "192.168.68.0/22"]}
    assert str(lanspace.real_addr("10.240.1.10", slots)) == "192.168.1.10"
    assert str(lanspace.real_addr("10.241.1.10", slots)) == "192.168.1.10"
    assert str(lanspace.real_addr("10.241.69.7", slots)) == "192.168.69.7"
    assert str(lanspace.real_addr("10.240.0.5", {1: ["10.0.0.0/16"]})) == "10.0.0.5"
    # туда и обратно
    for slot, real in ((1, "172.16.5.33"), (2, "192.168.71.254")):
        t = lanspace.twin_addr(slot, real)
        assert str(lanspace.real_addr(t, {1: ["172.16.5.0/24"], 2: ["192.168.68.0/22"]})) == real


@pytest.mark.parametrize("twin", ["10.240.2.10", "10.242.1.10", "10.255.53.53", "192.168.1.10", "мусор"])
def test_real_addr_unknown(twin):
    assert lanspace.real_addr(twin, {1: ["192.168.1.0/24"], 2: ["192.168.1.0/24"]}) is None


# ── routing.lan_space ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,want,bad", [
    ("", SPACE, False), (None, SPACE, False), ("  10.240.0.0/12 ", SPACE, False),
    ("10.0.0.0/12", "10.0.0.0/12", False), ("172.16.0.0/12", "172.16.0.0/12", False),
    ("10.240.0.0/16", SPACE, True), ("10.0.0.0/8", SPACE, True),
    ("100.64.0.0/12", SPACE, True), ("10.240.0.1/12", SPACE, True), ("мусор", SPACE, True),
])
def test_parse_space(raw, want, bad):
    net, why = lanspace.parse_space(raw)
    assert str(net) == want and bool(why) is bad, why


def test_space_from_config_and_bad_value_logged_once(monkeypatch, caplog):
    assert str(lanspace.space()) == SPACE                 # ключ в шаблоне закомментирован
    monkeypatch.setattr(config, "ROUTING_LAN_SPACE", "10.16.0.0/12")
    assert str(lanspace.space()) == "10.16.0.0/12"
    monkeypatch.setattr(config, "ROUTING_LAN_SPACE", "10.16.0.0/16")
    with caplog.at_level(logging.WARNING, logger="awgbot.util.lanspace"):
        assert str(lanspace.space()) == SPACE
        assert str(lanspace.space()) == SPACE
    assert len([r for r in caplog.records if "routing.lan_space" in r.getMessage()]) == 1


def test_lan_space_key_in_template_is_commented():
    text = (config.BASE_DIR / "conf" / "app.yaml").read_text(encoding="utf-8")
    assert re.search(r'^  # lan_space: "10\.240\.0\.0/12"$', text, re.M)
    assert not re.search(r"^  lan_space:", text, re.M)


# ── проверка пространства ────────────────────────────────────────────────────

_LINKS = ["10.99.99.0/30", "10.99.99.4/30"]


def test_space_problem_clean():
    assert lanspace.space_problem(SPACE, ["10.8.1.0/24", "10.9.1.0/24"], _LINKS,
                                  "10.255.53.53", "10.255.53.1") == ""


@pytest.mark.parametrize("clients", [["10.240.1.0/24"], ["10.8.1.0/24", "10.247.3.0/24"],
                                     ["10.0.0.0/8"]])
def test_space_problem_client_and_migration_subnets(clients):
    why = lanspace.space_problem(SPACE, clients, _LINKS, "10.255.53.53", "10.255.53.1")
    assert "подсетью пользователей" in why and clients[-1] in why, why


def test_space_problem_link():
    why = lanspace.space_problem(SPACE, ["10.8.1.0/24"], ["10.99.99.0/30", "10.250.0.4/30"])
    assert "подсетью линка 10.250.0.4/30" in why, why


@pytest.mark.parametrize("admin,intercept,bad", [
    ("10.255.53.53", "10.255.53.1", ""),              # в служебной /16
    ("10.53.0.53", "192.168.53.1", ""),               # вне пространства
    ("10.240.0.53", "10.255.53.1", "резолвер админа"),
    ("10.255.53.53", "10.254.53.1", "адрес перехвата DNS"),
    ("", "", ""), ("мусор", "мусор", ""),
])
def test_space_problem_service_addrs(admin, intercept, bad):
    why = lanspace.space_problem(SPACE, ["10.8.1.0/24"], _LINKS, admin, intercept)
    if bad:
        assert why.startswith(bad) and "вне служебных адресов 10.255.0.0/16" in why, why
    else:
        assert why == ""


def test_space_problem_other_space_uses_its_service_net():
    # 10.255.53.53 вне 10.32.0.0/12 — годится; внутри — только служебная 10.47/16
    sp = "10.32.0.0/12"
    assert lanspace.space_problem(sp, ["10.8.1.0/24"], _LINKS, "10.255.53.53", "10.47.53.1") == ""
    assert "адрес перехвата DNS 10.46.53.1" in lanspace.space_problem(sp, ["10.8.1.0/24"], _LINKS, "", "10.46.53.1")
    assert "10.8.1.0/24" in lanspace.space_problem("10.0.0.0/12", ["10.8.1.0/24"], _LINKS)


# ── адрес перехвата и сервисный вызов ────────────────────────────────────────

def test_intercept_addr_mirrors_host_script():
    from awgbot.infra import routing
    text = (config.BASE_DIR / "install" / "routing-host-setup.sh").read_text(encoding="utf-8")
    m = re.search(r'^DNS_ADDR="\$\{DNS_ADDR:-([0-9.]+)\}"', text, re.M)
    assert m and m.group(1) == routing.DNS_INTERCEPT_ADDR


def test_lan_space_problem_default_is_clean(services):
    assert services.lan_space_problem() == ""


def test_lan_space_problem_client_subnet(services, monkeypatch):
    monkeypatch.setattr(config, "ROUTING_CLIENT_SUBNET", "10.240.1.0/24")
    assert "подсетью пользователей 10.240.1.0/24" in services.lan_space_problem()


def test_lan_space_problem_migration_subnet(services, monkeypatch):
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.241.7")
    assert "подсетью пользователей 10.241.7.0/24" in services.lan_space_problem()


def test_lan_space_problem_link_of_slot(services, make_active_client, monkeypatch):
    monkeypatch.setattr(config, "ROUTING_LINK_CIDRS", ["10.99.99.0/30"])
    dev = services.add_device(make_active_client(device_limit=3).id, "gw")
    services.db.gateway_add(dev.device_id, "awglink2", 8443, "10.250.0.4/30", slot_id=2)
    assert "подсетью линка 10.250.0.4/30" in services.lan_space_problem()


def test_lan_space_problem_admin_resolver(services, monkeypatch):
    real = settings.get
    monkeypatch.setattr(settings, "get",
                        lambda k, d=None: "10.240.0.53" if k == "app.client_config.admin_dns" else real(k, d))
    assert services.lan_space_problem().startswith("резолвер админа 10.240.0.53")
