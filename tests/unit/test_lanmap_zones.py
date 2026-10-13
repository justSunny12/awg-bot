"""Зоны conntrack по неупорядоченной паре сторон пакета (таблица awg_lan_map,
цепочка zone): одинаковые настоящие адреса за разными шлюзами спорят не за
маршрут, а за запись conntrack — после перевода ответные кортежи двух
соединений совпадают. Пара сторон разводит их по зонам.

Цепочка проверяется исполнением её же текста: маленький разборщик правил
ниже идёт по ним сверху вниз, как ядро, до первого `accept`.

Цена ошибки: ответ получает не ту зону, что запрос, — соединение не встаёт
вовсе; ответ РФ-доступа из линка уходит из зоны 0 — РФ-доступ лежит у всех;
пакет из линка одного слота с адресом сети другого попадает в его зону —
скомпрометированный шлюз подмешивается в чужие соединения.
"""
from __future__ import annotations

import ipaddress
import itertools
import re

import pytest

from awgbot.infra.routing import lanmap

E = lanmap.Entry
# слоты 1 и 2 — одна и та же подсеть; слот 3 — своя
ENTRIES = [E(1, "awglink", "192.168.1.0/24", "10.240.1.0/24"),
           E(2, "awglink2", "192.168.1.0/24", "10.241.1.0/24"),
           E(3, "awglink3", "192.168.68.0/24", "10.242.68.0/24")]
PREFIXES = {1: "10.240.0.0/16", 2: "10.241.0.0/16", 3: "10.242.0.0/16"}
TEXT = lanmap.render(ENTRIES, "10.240.0.0/12", PREFIXES)
LOCAL = {"10.99.99.1", "10.99.99.5", "10.9.1.1", "203.0.113.1"}   # адреса самого ВПС
ADMIN = "10.9.1.5"


# ── свойства функции зоны ────────────────────────────────────────────────────

def test_the_zone_is_a_function_of_the_unordered_pair():
    pairs = list(itertools.product(range(16), repeat=2))
    for a, b in pairs:
        assert lanmap.zone(a, b) == lanmap.zone(b, a)
        assert 0 <= lanmap.zone(a, b) <= 0xFFFF
        assert (lanmap.zone(a, b) == 0) == (a == b == 0)
    zones = {lanmap.zone(a, b) for a, b in pairs if a <= b}
    assert len(zones) == 16 * 17 // 2, "разные пары — разные зоны"
    assert lanmap.zone(0, 1) == 2 and lanmap.zone(0, 2) == 3 and lanmap.zone(2, 1) == 19


# ── цепочка zone как её исполнит ядро ────────────────────────────────────────

def _sets(text: str) -> dict[str, list]:
    out = {}
    for name, elems in re.findall(r"set (\w+) \{[^}]*elements = \{ ([^}]*) \} \}", text):
        out[name] = [ipaddress.ip_network(e.strip()) for e in elems.split(",")]
    return out


def _zone_rules(text: str) -> list[str]:
    body = text.split("chain slot_zone {", 1)[1].split("\n    }", 1)[0]
    return [ln.strip() for ln in body.splitlines()[2:] if ln.strip()]


def classify(iif: str, saddr: str, daddr: str) -> int:
    """Зона пакета на входе ВПС по тексту цепочки zone; без правила — 0."""
    sets = _sets(TEXT)
    s, d = ipaddress.ip_address(saddr), ipaddress.ip_address(daddr)
    for rule in _zone_rules(TEXT):
        pre = re.fullmatch(r'iifname != \{ (.*) \} ip daddr != (\S+) accept', rule)
        if pre:
            links = [x.strip().strip('"') for x in pre.group(1).split(",")]
            if iif not in links and d not in ipaddress.ip_network(pre.group(2)):
                return 0
            continue
        if rule == "fib daddr type local accept":
            if daddr in LOCAL:
                return 0
            continue
        m = re.fullmatch(r'(?:iifname "(\S+)" )?(?:ip saddr @(\w+) )?(?:ip daddr (\S+) )?ct zone set (\d+) accept', rule)
        assert m, f"правило чужой формы: {rule}"
        r_if, r_set, r_dst, zone = m.groups()
        if r_if and r_if != iif:
            continue
        if r_set and not any(s in n for n in sets[r_set]):
            continue
        if r_dst and d not in ipaddress.ip_network(r_dst):
            continue
        return int(zone)
    return 0


@pytest.mark.parametrize("what, query, reply, zone", [
    ("админ → сеть слота 1", ("awg1", ADMIN, "10.240.1.10"), ("awglink", "192.168.1.10", ADMIN), 2),
    ("админ → сеть слота 2, тот же адрес", ("awg1", ADMIN, "10.241.1.10"), ("awglink2", "192.168.1.10", ADMIN), 3),
    ("сеть слота 1 → админ", ("awglink", "192.168.1.10", ADMIN), ("awg1", ADMIN, "10.240.1.10"), 2),
    ("сеть слота 2 → сеть слота 1", ("awglink2", "192.168.1.20", "10.240.1.10"),
     ("awglink", "192.168.1.10", "10.241.1.20"), 19),
    ("сеть слота 3 → сеть слота 1", ("awglink3", "192.168.68.7", "10.240.1.10"),
     ("awglink", "192.168.1.10", "10.242.68.7"), 20),
    ("РФ-доступ: клиент → сайт, ответ из линка", ("awg1", "10.9.1.7", "203.0.113.3"),
     ("awglink", "203.0.113.3", "10.9.1.7"), 0),
    ("DNS клиента к резолверу ВПС", ("awg1", "10.9.1.7", "10.9.1.1"), ("lo", "10.9.1.1", "10.9.1.7"), 0),
])
def test_a_reply_gets_the_zone_of_its_query(what, query, reply, zone):
    """Для ответа стороны меняются местами: пара та же — зона та же.
    Направленные зоны не нужны."""
    assert classify(*query) == zone, what
    assert classify(*reply) == zone, what


def test_the_same_address_behind_two_slots_lands_in_two_zones():
    """(а) из концепта: один исходный порт к двум NAS 192.168.1.10 за разными
    шлюзами — ответные кортежи после перевода совпадают, зоны — нет."""
    assert classify("awg1", ADMIN, "10.240.1.10") != classify("awg1", ADMIN, "10.241.1.10")
    # (б): одинаковые источники из двух сетей к админу
    assert classify("awglink", "192.168.1.10", ADMIN) != classify("awglink2", "192.168.1.10", ADMIN)


def test_traffic_to_the_vps_itself_stays_in_zone_0():
    """Канал и зонд линка (адрес ВПС в /30), сеть шлюза к адресу ВПС — зона 0."""
    assert classify("awglink", "10.99.99.2", "10.99.99.1") == 0
    assert classify("awglink", "192.168.1.10", "10.99.99.1") == 0


def test_a_source_of_another_slot_from_this_link_is_not_its_zone():
    """Сторона-источник привязана к линку: адрес сети слота 3, пришедший
    линком слота 1, в зону слота 3 не попадает — и в чужие записи тоже."""
    spoofed = classify("awglink", "192.168.68.7", ADMIN)
    assert spoofed == 0 and spoofed != classify("awglink3", "192.168.68.7", ADMIN)
    assert classify("awglink", "192.168.68.7", "10.241.1.10") == lanmap.zone(0, 2)


def test_pairs_come_before_single_sides_in_the_chain():
    """Каждое правило завершает цепочку: одиночное «из сети слота» выше пары
    перезаписало бы зону пары."""
    rules = _zone_rules(TEXT)
    assert rules[0] == 'iifname != { "awglink", "awglink2", "awglink3" } ip daddr != 10.240.0.0/12 accept', \
        "прочий трафик хоста — первым правилом, без разбора пар"
    assert rules[1] == "fib daddr type local accept"
    kinds = ["pair" if "ip saddr" in r and "ip daddr" in r else "src" if "ip saddr" in r else "dst"
             for r in rules[2:]]
    assert kinds == sorted(kinds, key=["pair", "src", "dst"].index)
    assert kinds.count("pair") == 6 and kinds.count("src") == 3 and kinds.count("dst") == 3
