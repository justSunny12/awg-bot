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
    assert fake_routing.lanmap and two.db.get_state("lanmap_text") == fake_routing.lanmap
    assert fake_routing.dropped_routes == [("192.168.1.0/24", "awglink"), ("192.168.1.0/24", "awglink2")]


def test_a_table_that_did_not_go_in_keeps_the_real_routes_and_is_said(two, fake_routing, monkeypatch):
    """nft отказал — прежние маршруты настоящих подсетей не трогаем (иначе
    админ остался бы без сетей вовсе), причина — в state, карточке слота и
    замечании при запуске; поставилась на следующей сверке — строки уходят."""
    from awgbot.bot.texts.routing.card import lan_space_lines
    from awgbot.runtime import preflight
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")

    def refuse(text):
        raise routing.RoutingError("nft -c -f -: Error: Could not process rule: No such file or directory")
    monkeypatch.setattr(routing.lanmap, "apply", refuse)
    two.lan_map_startup()
    assert fake_routing.lanmap is None and fake_routing.dropped_routes == []
    card = two.lan_space_card(two.db.gateway(1))
    assert card["error"].startswith("nft -c -f -: Error")
    assert lan_space_lines(card) == [
        "⚠️ Адреса в туннеле не работают: таблица awg_lan_map не поставлена (nft -c -f -: Error: Could not "
        "process rule: No such file or directory) — журнал: <code>journalctl -u awg-bot | grep 'адреса в "
        "туннеле'</code>"]
    warns = preflight.collect_warnings(two, server_ok=True)
    assert ("адреса в туннеле не работают: таблица awg_lan_map не поставлена (nft -c -f -: Error: Could not "
            "process rule: No such file or directory) — журнал: journalctl -u awg-bot | grep 'адреса в "
            "туннеле'") in warns, warns
    monkeypatch.setattr(routing.lanmap, "apply", lambda text: setattr(fake_routing, "lanmap", text))
    two.lan_map_ensure()
    assert two.lan_space_card(two.db.gateway(1))["error"] == "" and two.lan_map_up()


def test_an_empty_table_is_taken_down_when_it_is_still_there(two, fake_routing):
    """Подсетей не осталось, а таблица в ядре стоит (state потерян, восстановлен
    из копии) — снимается, а не остаётся половиной механизма."""
    fake_routing.lanmap = "старая таблица"
    two.db.gateway_update(1, lan_subnets=[]); two.db.gateway_update(2, lan_subnets=[])
    two.lan_map_ensure()
    assert fake_routing.lanmap is None and fake_routing.lanmap_applies == []


def test_a_fwmark_on_the_slot_bits_turns_the_addresses_off(two, fake_routing, monkeypatch):
    """Метка РФ-доступа на битах слотов увела бы помеченное в чужой линк:
    адреса в туннеле выключены, причина и ключ для починки — в карточке."""
    from awgbot.bot.texts.routing.card import lan_space_lines
    two.lan_map_ensure()
    monkeypatch.setattr(config, "ROUTING_FWMARK", 4)
    two.lan_map_ensure()
    assert fake_routing.lanmap is None
    card = two.lan_space_card(two.db.gateway(1))
    assert card["key"] == "routing.fwmark"
    assert lan_space_lines(card) == [
        "⚠️ Адреса в туннеле выключены: метка РФ-доступа routing.fwmark 0x4 задевает биты меток слотов 0x6 — "
        "поправь <code>routing.fwmark</code> в app.yaml"]


def test_slot_policy_is_not_asserted_twice_in_one_tick(two, fake_routing, monkeypatch):
    """Задействован РФ-доступ — политику слотов утверждает он (ensure_policy);
    сверка адресов в туннеле в тот же такт её не повторяет. Не задействован —
    ставит сама."""
    monkeypatch.setattr(two, "routing_engaged", lambda: True)
    monkeypatch.setattr(two, "_ensure_gateway_policy", lambda: two.policy.append("rf"))
    two.routing_liveness_tick()
    assert two.policy == ["rf"], two.policy
    assert fake_routing.lanmap, "таблица в первый такт — всё равно"


def test_a_broken_space_keeps_quiet_about_the_linked_networks(two, monkeypatch):
    """Пока адреса в туннеле выключены, связи подсетей нет — хвост не шлётся
    и ключ не взводится: скажется, когда пространство починят."""
    _legacy_toggle(monkeypatch, False)
    two.db.gateway_update(1, lan_mode=1); two.db.gateway_update(2, lan_mode=1)
    monkeypatch.setattr(config, "ROUTING_LAN_SPACE", "10.0.0.0/12")
    assert two.lan_space_announce() == [] and not two.db.get_state("lan_peers_announced")
    assert two.gateway_peer_nets_info() == {"state": "off"}
    monkeypatch.setattr(config, "ROUTING_LAN_SPACE", "")
    assert len(two.lan_space_announce()) == 1


def test_an_old_subnet_over_a_neighbours_prefix_says_the_link_will_not_build(two):
    """Подсеть до 3.3.0 в пространстве, накрывающая префикс соседа с
    VPN-транзитом: на шлюзе она пересеклась бы с его AllowedIPs — конфиг
    линка не соберётся, пока её не сменят; это надо сказать."""
    from awgbot.bot.texts.routing.card import lan_space_lines
    two.db.gateway_update(1, lan_subnets=["192.168.1.0/24", "10.241.0.0/24"], lan_mode=1)
    two.db.gateway_update(2, lan_mode=1)
    lines = lan_space_lines(two.lan_space_card(two.db.gateway(1)))
    assert lines == ["⚠️ <code>10.241.0.0/24</code> — в служебных адресах сервера AWG, в туннеле не видна, и конфиг "
                     "линка не соберётся (пересекается с адресами в туннеле соседа): смени подсеть в «🗺 Подсети»"]
    two.db.gateway_update(2, lan_mode=0)
    assert "конфиг линка" not in lan_space_lines(two.lan_space_card(two.db.gateway(1)))[0]


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
        assert two.db.get_state("lan_peers_announced") == "1"
        assert not two.db.get_state("lan_space_announced"), "ключ полного уведомления — не этого хвоста"
        return
    assert len(notes) == 1
    assert ("Связь подсетей теперь часть VPN-транзита, отдельного выключателя больше нет: сети NASPi и Pi2 "
            "связаны") == notes[0].text
    assert two.lan_space_announce() != [], "не доставлено — скажется на следующем старте"
    notes[0].on_sent()
    assert two.lan_space_announce() == []
