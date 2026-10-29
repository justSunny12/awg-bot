"""Адреса ответчика и зондов узлов кворума в подблоке линии
(util/probenet.py: responder_addr, responder_cidr, node_addr, node_of_addr,
other_side, MAX_SLOT; концепт «кворум» §3.1).

Цена ошибки: ответчик сверяет узел пробы с адресом источника — адрес зонда не
на своём месте, и проба шлюза засчитывается за другой узел (голос дважды) или
не засчитывается вовсе (лидер лишается кворума на ровном месте); слот, вылезший
за /26, кладёт зонд в чужую линию; шлюз чужой стороны с адресом в линии голосует
о чужом сервере вопреки составу.
"""
from __future__ import annotations

import ipaddress

import pytest

from awgbot.util import probenet

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("side,clone,addr", [
    ("a", False, "169.254.88.1"), ("a", True, "169.254.88.2"),
    ("b", False, "169.254.88.65"), ("b", True, "169.254.88.66"),
])
def test_responder_addresses_follow_the_table(side, clone, addr):
    assert probenet.responder_addr(side, clone=clone) == addr
    assert probenet.responder_cidr(side, clone=clone) == addr + "/26", \
        "второй адрес интерфейса — /26: маршрут на весь блок линии"


@pytest.mark.parametrize("side,node,addr", [
    ("a", "a", "169.254.88.3"), ("a", "b", "169.254.88.4"),
    ("a", "a1", "169.254.88.5"), ("a", "a2", "169.254.88.6"), ("a", "a58", "169.254.88.62"),
    ("b", "b", "169.254.88.67"), ("b", "a", "169.254.88.68"),
    ("b", "b1", "169.254.88.69"), ("b", "b58", "169.254.88.126"),
])
def test_node_addresses_follow_the_table(side, node, addr):
    """.3 — сервер линии, .4 — второй сервер, .4 + N — шлюз слота N."""
    assert probenet.node_addr(side, node) == addr


def test_every_node_address_stays_inside_its_line_and_below_the_broadcast():
    for side in ("a", "b"):
        net = probenet.line_net(side)
        nodes = [side, probenet.other_side(side)] + [f"{side}{n}" for n in range(1, probenet.MAX_SLOT + 1)]
        addrs = [probenet.node_addr(side, n) for n in nodes]
        assert len(set(addrs)) == len(addrs), "два узла на одном адресе"
        for a in addrs:
            ip = ipaddress.ip_address(a)
            assert ip in net and ip != net.broadcast_address, f"{a} вне подблока линии {side}"
        assert probenet.responder_addr(side) not in addrs and probenet.responder_addr(side, True) not in addrs


@pytest.mark.parametrize("side,node", [("a", "a59"), ("b", "b59"), ("a", "a0"), ("a", "a100")])
def test_a_slot_that_does_not_fit_the_slash_26_is_refused(side, node):
    with pytest.raises(ValueError):
        probenet.node_addr(side, node)


@pytest.mark.parametrize("side,node", [("a", "b1"), ("b", "a1"), ("a", "c"), ("a", ""), ("a", "x1"), ("a", "1")])
def test_a_gateway_of_the_other_side_or_garbage_has_no_address_in_the_line(side, node):
    """Шлюз «Beta» о линии «Alpha» не голосует (у него своя линия)."""
    with pytest.raises(ValueError):
        probenet.node_addr(side, node)


def test_node_of_addr_reverses_node_addr_for_every_node():
    for side in ("a", "b"):
        for node in [side, probenet.other_side(side)] + [f"{side}{n}" for n in range(1, probenet.MAX_SLOT + 1)]:
            assert probenet.node_of_addr(side, probenet.node_addr(side, node)) == node


@pytest.mark.parametrize("side,ip", [
    ("a", "169.254.88.0"), ("a", "169.254.88.1"), ("a", "169.254.88.2"), ("a", "169.254.88.63"),
    ("a", "169.254.88.69"), ("b", "169.254.88.5"), ("b", "169.254.88.65"), ("b", "169.254.88.127"),
    ("a", "10.0.0.5"), ("a", "мусор"), ("a", ""), ("a", "::1"),
])
def test_an_address_that_is_not_a_probe_of_the_line_is_no_node(side, ip):
    """Ответчики, сеть и широковещательный адрес подблока, адреса другой линии и
    мусор — не узел: проба с такого источника не считается."""
    assert probenet.node_of_addr(side, ip) == ""


def test_other_side_flips_and_refuses_garbage():
    assert probenet.other_side("a") == "b" and probenet.other_side("b") == "a"
    with pytest.raises(ValueError):
        probenet.other_side("c")
    with pytest.raises(ValueError):
        probenet.responder_addr("c")
