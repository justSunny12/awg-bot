"""Блок адресов пиров-зондов кворума (util/probenet.py): 169.254.88.0/24.

Сверка пиров, онлайн-счётчик и часть VPN пакета переезда узнают зонд только
по `is_probe`. Цена ошибки в обе стороны: блок шире, чем надо, — бот
перестанет видеть настоящее устройство (чужой пир без тревоги, устройство
не в онлайне, клон без пользователя); уже, чем надо, — откат с выпуска с
кворумом поднимет «🚨 Чужой пир в конфиге сервера» на каждый зонд и
посадит их в карантин.
"""
from __future__ import annotations

import ipaddress

import pytest

from awgbot.util import probenet

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("ip", [
    "169.254.88.0", "169.254.88.1", "169.254.88.5", "169.254.88.255",
    "169.254.88.5/32", "169.254.88.1/26", "169.254.88.65/26",
    " 169.254.88.6 ", "169.254.88.4/32\n",
])
def test_addresses_of_the_block_with_or_without_a_prefix_are_probes(ip):
    assert probenet.is_probe(ip), f"{ip!r} лежит в блоке зондов, а зондом не признан"


@pytest.mark.parametrize("ip", [
    "169.254.87.255", "169.254.89.0",              # соседи блока по краям
    "169.254.0.1", "169.254.1.88",                 # тот же link-local, но не блок
    "10.8.0.88", "10.8.0.2/32", "169.254.88",      # пользователи и обрубок
    "169.254.88.256", "169.254.88.5/33", "зонд", "169.254.88.5; rm",
    "", "   ", None,
    "fe80::1", "::ffff:169.254.88.5", "fd00:88::5/128",
])
def test_neighbours_users_garbage_ipv6_and_empty_are_not_probes(ip):
    assert not probenet.is_probe(ip), f"{ip!r} не зонд, а признан зондом — устройство выпадет из учёта"


def test_is_probe_never_raises_on_odd_input():
    """Вызывают на каждом пире каждой сверки: исключение из разбора адреса
    уронило бы сверку целиком, а не один пир."""
    for v in (0, 123, b"169.254.88.5", object()):
        probenet.is_probe(v)


def test_the_block_is_exactly_a_slash_24():
    assert probenet.PROBE_NET == ipaddress.ip_network("169.254.88.0/24")


def test_line_blocks_are_two_disjoint_quarters_inside_the_probe_block():
    """Подблоки линий: «a» — основа пары, «b» — вторая сторона. Пересекись
    они — зонды двух линий получили бы одинаковые адреса на одном сервере."""
    a, b = probenet.line_net("a"), probenet.line_net("b")
    assert a == ipaddress.ip_network("169.254.88.0/26")
    assert b == ipaddress.ip_network("169.254.88.64/26")
    assert not a.overlaps(b), "подблоки линий пересекаются"
    assert a.subnet_of(probenet.PROBE_NET) and b.subnet_of(probenet.PROBE_NET)
    # второй адрес интерфейса каждой линии — зонд
    assert probenet.is_probe(str(next(a.hosts())) + "/26") and probenet.is_probe(str(next(b.hosts())) + "/26")


def test_a_comment_at_the_end_of_the_value_does_not_hide_a_probe():
    """4.2.0 подписывает свои строки («… # кворум»): с комментарием адрес блока
    перестал бы узнаваться, и зонд пошёл бы в карантин с тревогой."""
    assert probenet.is_probe("169.254.88.5/32 # кворум")
    assert probenet.is_probe("169.254.88.1/26#q")
    assert not probenet.is_probe("10.8.1.5/32 # 169.254.88.5"), "адрес из комментария принят за значение"


@pytest.mark.parametrize("side", ["c", "", "A", " a", "ab", None, 0])
def test_an_unknown_line_side_is_an_error_not_the_second_quarter(side):
    """Любая сторона, кроме «a», раньше молча получала подблок «b»: опечатка в
    вызывающем коде дала бы обеим сторонам линии одни и те же адреса зондов."""
    with pytest.raises(ValueError):
        probenet.line_net(side)


# ── значения AllowedIPs/Address и правило «зонд» ─────────────────────────────

@pytest.mark.parametrize("lines, expected", [
    ("10.8.1.1/24", ["10.8.1.1/24"]),
    ("10.8.1.1/24, 169.254.88.1/26", ["10.8.1.1/24", "169.254.88.1/26"]),
    ("10.8.1.1/24,169.254.88.1/26 # кворум", ["10.8.1.1/24", "169.254.88.1/26"]),
    (["10.8.1.1/24", " 169.254.88.1/26 "], ["10.8.1.1/24", "169.254.88.1/26"]),
    (["10.8.1.1/24, fd00::1/64", "169.254.88.65/26"], ["10.8.1.1/24", "fd00::1/64", "169.254.88.65/26"]),
    ("", []), ("  ,  , ", []), ("# только комментарий", []), ([], []),
])
def test_values_flattens_lines_and_commas_and_drops_comments(lines, expected):
    assert probenet.values(lines) == expected


@pytest.mark.parametrize("allowed", [
    "169.254.88.5/32",
    "169.254.88.5/32 # зонд",
    "169.254.88.5/32, 169.254.88.6/32",
    ["169.254.88.5/32", "169.254.88.70/32"],
])
def test_a_peer_whose_every_allowed_ip_is_in_the_block_is_a_probe(allowed):
    assert probenet.peer_is_probe(allowed, ["10.8.1.1/24"]), f"{allowed!r} — зонд, а не признан"
    assert probenet.peer_is_probe(allowed), "без подсети пользователей зонд тоже зонд"


@pytest.mark.parametrize("allowed", [
    "169.254.88.5/32, 10.8.1.77/32",               # через запятую в одной строке
    "10.8.1.77/32,169.254.88.5/32",
    ["169.254.88.5/32", "10.8.1.77/32"],            # разными строками
    ["169.254.88.5/32", "0.0.0.0/0"],               # весь интернет через «зонд»
    "169.254.88.5/32, ::/0",
    "169.254.88.5/32, мусор",
    # значение с адресом блока, но шире него: wg маршрутизирует всю сеть
    # значения (у «169.254.88.9/0» — весь интернет), а не один адрес
    "169.254.88.0/23", "169.254.88.9/16", "169.254.88.9/0",
    "", [], "# пусто",
])
def test_a_peer_with_anything_besides_block_addresses_is_not_a_probe(allowed):
    """Пир «адрес блока + ещё что-то» — лазейка: под видом зонда чужой получил
    бы доступ в подсеть пользователей или весь интернет, а сторож «чужой пир»
    промолчал бы."""
    assert not probenet.peer_is_probe(allowed, ["10.8.1.1/24"]), f"{allowed!r} признан зондом"


@pytest.mark.parametrize("user_nets", [
    ["169.254.88.1/24"], ["169.254.0.1/16"], ["169.254.88.200/29"],
    ["10.8.1.1/24", "169.254.88.1/24"],            # вторая подсеть пользователей (переезд) в блоке
    ["169.254.88.0/24 # пользователи"],
])
def test_a_users_subnet_overlapping_the_block_switches_the_probe_rule_off(user_nets):
    """Подсеть пользователей в 169.254.88.x: устройства там — настоящие, и
    правило зондов спрятало бы их от сверки, онлайна и клона."""
    assert probenet.overlaps(user_nets)
    assert not probenet.peer_is_probe("169.254.88.5/32", user_nets), \
        f"при подсети пользователей {user_nets} устройство 169.254.88.5 признано зондом"


@pytest.mark.parametrize("user_nets", [
    [], ["10.8.1.1/24"], ["169.254.87.1/24"], ["169.254.89.1/24"], ["fd00::1/64"], ["мусор", ""],
])
def test_a_users_subnet_beside_the_block_keeps_the_rule(user_nets):
    assert not probenet.overlaps(user_nets), f"{user_nets} не пересекает блок"
    assert probenet.peer_is_probe("169.254.88.5/32", user_nets)
