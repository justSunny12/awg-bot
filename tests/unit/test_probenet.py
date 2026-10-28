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
