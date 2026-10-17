"""Адреса в туннеле на ВПС (сервис LanMapMixin): сверка таблицы awg_lan_map с
таблицами и правилами слотов, независимо от РФ-доступа; старт — таблица до
первого такта; неисправное пространство; снятие слота; потолок слотов;
замечание при запуске.

Цена ошибки: таблица стоит только при задействованном РФ-доступе — админ без
РФ-доступа теряет сети шлюзов; пространство поверх подсети клиентов —
сервер переводит адреса собственных клиентов.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config
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


# ── старт: таблица ───────────────────────────────────────────────────────────

def test_startup_puts_the_table(two, fake_routing, monkeypatch):
    """Старт: таблица адресов в туннеле — на место до первого такта (разовая
    уборка маршрутов настоящих подсетей 3.2.x снята со сдвигом минимума)."""
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    two.lan_map_startup()
    assert fake_routing.lanmap and two.db.get_state("lanmap_text") == fake_routing.lanmap


def test_a_table_that_did_not_go_in_keeps_the_real_routes_and_is_said(two, fake_routing, monkeypatch):
    """nft отказал — причина в state, карточке слота и
    замечании при запуске; поставилась на следующей сверке — строки уходят."""
    from awgbot.bot.texts.routing.card import lan_space_lines
    from awgbot.runtime import preflight
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")

    def refuse(text):
        raise routing.RoutingError("nft -c -f -: Error: Could not process rule: No such file or directory")
    monkeypatch.setattr(routing.lanmap, "apply", refuse)
    two.lan_map_startup()
    assert fake_routing.lanmap is None
    card = two.lan_space_card(two.db.gateway(1))
    assert card["error"].startswith("nft -c -f -: Error")
    assert lan_space_lines(card) == [
        "⚠️ Адреса в туннеле не работают: nft не принял таблицу <code>awg_lan_map</code> — подробности: "
        "<code>journalctl -u awg-bot -o short-iso | grep 'адреса в туннеле'</code>"]
    warns = preflight.collect_warnings(two, server_ok=True)
    assert ("адреса в туннеле не работают: таблица awg_lan_map не поставлена (nft -c -f -: Error: Could not "
            "process rule: No such file or directory) — журнал: journalctl -u awg-bot -o short-iso | grep 'адреса в "
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
        "⚠️ Адреса в туннеле выключены: метка РФ-доступа 0x4 задевает биты меток слотов 0x6 — "
        "поправь <code>routing.fwmark</code> в <code>app.yaml</code>"]


def test_slot_policy_is_not_asserted_twice_in_one_tick(two, fake_routing, monkeypatch):
    """Задействован РФ-доступ — политику слотов утверждает он (ensure_policy);
    сверка адресов в туннеле в тот же такт её не повторяет. Не задействован —
    ставит сама."""
    monkeypatch.setattr(two, "routing_engaged", lambda: True)
    monkeypatch.setattr(two, "_ensure_gateway_policy", lambda: two.policy.append("rf"))
    two.routing_liveness_tick()
    assert two.policy == ["rf"], two.policy
    assert fake_routing.lanmap, "таблица в первый такт — всё равно"


def test_an_old_subnet_over_a_neighbours_prefix_says_the_link_will_not_build(two):
    """Подсеть до 3.3.0 в пространстве, накрывающая префикс соседа с
    VPN-транзитом: на шлюзе она пересеклась бы с его AllowedIPs — конфиг
    линка не соберётся, пока её не сменят; это надо сказать."""
    from awgbot.bot.texts.routing.card import lan_space_lines
    two.db.gateway_update(1, lan_subnets=["192.168.1.0/24", "10.241.0.0/24"], lan_mode=1)
    two.db.gateway_update(2, lan_mode=1)
    lines = lan_space_lines(two.lan_space_card(two.db.gateway(1)))
    assert lines == ["⚠️ <code>10.241.0.0/24</code> — в служебном пространстве сервера AWG, в туннеле не видна, и конфиг "
                     "линка не соберётся (пересекается с адресами в туннеле другого шлюза): смени подсеть в «🗺 Подсети»"]
    two.db.gateway_update(2, lan_mode=0)
    assert "конфиг линка" not in lan_space_lines(two.lan_space_card(two.db.gateway(1)))[0]


def test_a_stale_error_goes_away_when_the_table_is_already_right(two, fake_routing):
    """Отказ прошлой сверки, а таблица уже та, что нужно (поставили руками или
    отказ был временным): ранний выход сверки тоже снимает строку отказа."""
    two.lan_map_ensure()
    two.db.set_state("lanmap_error", "временный отказ")
    n = len(fake_routing.lanmap_applies)
    two.lan_map_ensure()
    assert len(fake_routing.lanmap_applies) == n and not two.db.get_state("lanmap_error")
    two.db.gateway_update(1, lan_subnets=[]); two.db.gateway_update(2, lan_subnets=[])
    two.lan_map_ensure()
    two.db.set_state("lanmap_error", "временный отказ")
    two.lan_map_ensure()
    assert not two.db.get_state("lanmap_error"), "пусто к пустому — отказа нет"


def test_an_empty_table_looks_into_the_kernel_only_once_and_a_missing_nft_is_no_error(two, monkeypatch):
    """Подсетей нет и не было: в ядро смотрим только первой сверкой процесса
    (таблица могла остаться от прошлой жизни); nft на хосте нет вовсе (docker
    без nftables) — таблицы нет, это не отказ."""
    looks = []

    def present():
        looks.append(1)
        raise routing.RoutingUnavailable("nft не найден — поставь пакет nftables")
    monkeypatch.setattr(routing.lanmap, "present", present)
    two.db.gateway_update(1, lan_subnets=[]); two.db.gateway_update(2, lan_subnets=[])
    assert two.lan_map_ensure() is True and not two.db.get_state("lanmap_error")
    two.lan_map_ensure(); two.lan_map_ensure()
    assert looks == [1], "в ядро смотрели не только на первой сверке"
