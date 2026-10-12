"""Адреса в туннеле на ВПС (сервис LanMapMixin): сверка таблицы awg_lan_map с
таблицами и правилами слотов, независимо от РФ-доступа; старт — уборка
маршрутов настоящих подсетей 3.2.x и разовое уведомление; неисправное
пространство; снятие слота; потолок слотов; замечание при запуске.

Цена ошибки: таблица стоит только при задействованном РФ-доступе — админ без
РФ-доступа теряет сети шлюзов; маршруты 3.2.x не убраны — настоящие адреса
работают до ребута (полупереход); пространство поверх подсети клиентов —
сервер переводит адреса собственных клиентов.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config, settings
from awgbot.domain.services import ServiceError
from awgbot.infra import routing

pytestmark = pytest.mark.integration
ADMIN = config.ADMIN_ID


@pytest.fixture()
def two(services, fake_routing, make_active_client, monkeypatch):
    """Два слота с подсетями (у слота 2 — та же, что у слота 1), политика слотов
    записывается."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    services.db.gateway_update(1, lan_subnets=["192.168.1.0/24"])
    services.db.gateway_update(2, lan_subnets=["192.168.1.0/24"])
    policy: list = []
    monkeypatch.setattr(routing.slots, "ensure_slot_policy", lambda sid, iface: policy.append((sid, iface)))
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    services.policy = policy
    return services


def test_the_table_and_slot_policy_do_not_wait_for_rf_access(two, fake_routing):
    """РФ-доступ выключен (обвязка недоступна) — такт живости всё равно ставит
    таблицы слотов и таблицу адресов в туннеле: доступ админа к сетям шлюзов
    от РФ-доступа не зависит."""
    fake_routing.enabled = False
    assert two.routing_liveness_tick() == []
    assert fake_routing.lanmap and "10.241.1.0/24 : 192.168.1.0/24" in fake_routing.lanmap
    assert two.policy == [(1, "awglink"), (2, "awglink2")]


def test_a_matching_table_is_not_reapplied_but_a_lost_one_is(two, fake_routing):
    """Сверка дешёвая: текст против state и список таблиц ядра. Совпало и
    стоит — ничего; таблицу сняли руками — ставится заново тем же текстом."""
    assert two.lan_map_ensure() is True
    n = len(fake_routing.lanmap_applies)
    two.lan_map_ensure()
    assert len(fake_routing.lanmap_applies) == n, "та же таблица перевыставлена"
    fake_routing.lanmap = None
    two.lan_map_ensure()
    assert len(fake_routing.lanmap_applies) == n + 1 and fake_routing.lanmap == fake_routing.lanmap_applies[-1]


def test_only_every_tenth_tick_looks_at_the_table(two, fake_routing):
    two.lan_map_tick()                                   # первый такт — сверка
    assert len(fake_routing.lanmap_applies) == 1
    fake_routing.lanmap = None
    for _ in range(9):
        two.lan_map_tick()
    assert fake_routing.lanmap is None, "сверка чаще раза в 10 тактов"
    two.lan_map_tick()
    assert fake_routing.lanmap is not None


def test_a_broken_space_turns_the_addresses_off_entirely(two, fake_routing, monkeypatch):
    """Пространство задевает подсеть клиентов — таблица снята, подсети соседей
    пусты, карточка и замечание при запуске говорят причину. Половина
    пространства хуже, чем ни одного."""
    two.db.gateway_update(1, lan_mode=1); two.db.gateway_update(2, lan_mode=1)
    two.lan_map_ensure()
    assert two.gateway_peer_nets(1) == ["10.241.0.0/16"]
    monkeypatch.setattr(config, "ROUTING_LAN_SPACE", "10.0.0.0/12")
    two.lan_map_ensure()
    assert fake_routing.lanmap is None and two.db.get_state("lanmap_text") == ""
    assert two.gateway_peer_nets(1) == [] and two.lan_twins(two.db.gateway(1)) == []
    problem = two.lan_space_card(two.db.gateway(1))["problem"]
    assert problem == "служебное пространство 10.0.0.0/12 пересекается с подсетью клиентов 10.8.1.0/24", problem
    from awgbot.runtime import preflight
    warns = preflight.collect_warnings(two, server_ok=True)
    assert ("адреса в туннеле выключены: служебное пространство 10.0.0.0/12 пересекается с подсетью "
            "клиентов 10.8.1.0/24 — поправь routing.lan_space в app.yaml") in warns, warns


def test_removing_a_slot_takes_its_twins_out_of_the_table(two, fake_routing):
    two.lan_map_ensure()
    assert "10.241.1.0/24" in fake_routing.lanmap
    two.gateway_remove(2)
    assert "10.241.1.0/24" not in fake_routing.lanmap and "10.240.1.0/24 : 192.168.1.0/24" in fake_routing.lanmap


def test_more_than_fifteen_slots_is_refused(services, monkeypatch):
    """Слоту 16 адресов в туннеле не хватает: /16 на слот, 15 слотов и служебная."""
    monkeypatch.setattr(config, "ROUTING_GATEWAYS_MAX", 16)
    slots = [types.SimpleNamespace(id=n, link_if=f"awglink{n}", link_port=1000 + n, link_cidr=f"10.99.{n}.0/30")
             for n in range(1, 16)]
    monkeypatch.setattr(services.db, "gateways", lambda: slots)
    with pytest.raises(ServiceError, match="не больше 15"):
        services.gateway_next_slot()


# ── старт: уборка маршрутов 3.2.x и уведомление ──────────────────────────────

def test_startup_drops_the_real_routes_and_puts_the_table(two, fake_routing, monkeypatch):
    """Маршруты настоящих подсетей, поставленные 3.2.x, живы до ребута: без
    уборки настоящие адреса у полного туннеля работали бы до него."""
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    two.lan_map_startup()
    assert fake_routing.dropped_routes == [("192.168.1.0/24", "awglink"), ("192.168.1.0/24", "awglink2")]
    assert fake_routing.lanmap and two.db.get_state("lanmap_text") == fake_routing.lanmap


def _legacy_toggle(monkeypatch, on: bool):
    real = settings.get_bool
    monkeypatch.setattr(settings, "get_bool",
                        lambda k, d=False: on if k == "app.routing.peer_nets.enabled" else real(k, d))


@pytest.mark.parametrize("was_on, transit, told", [
    (False, (1, 1), True),       # тумблер был выключен, два транзита — сети связались без вопроса
    (True, (1, 1), False),       # был включён — для человека ничего не изменилось
    (False, (1, 0), False),      # транзит у одного — связывать не с кем
])
def test_the_announcement_says_once_that_the_networks_are_now_linked(two, monkeypatch, was_on, transit, told):
    """Связь подсетей стала частью VPN-транзита; мёртвый ключ прежнего тумблера
    читается ровно здесь, один раз."""
    _legacy_toggle(monkeypatch, was_on)
    two.db.gateway_update(1, lan_mode=transit[0]); two.db.gateway_update(2, lan_mode=transit[1])
    notes = two.lan_space_announce()
    if not told:
        assert notes == [] and two.lan_space_announce() == []
        assert two.db.get_state("lan_space_announced") == "1"
        return
    assert len(notes) == 1
    assert ("Связь подсетей теперь часть VPN-транзита, отдельного выключателя больше нет: сети NASPi и Pi2 "
            "связаны") == notes[0].text
    assert two.lan_space_announce() != [], "не доставлено — скажется на следующем старте"
    notes[0].on_sent()
    assert two.lan_space_announce() == []
