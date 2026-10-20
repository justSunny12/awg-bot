"""Часть VPN пакета переезда (util/rsvpkg/vpn.py): из живого конфига
клиентского интерфейса — только данные для клона, без хуков и путей; на
резерве — проверка чужой части и сборка конфига клона из проверенных полей.

Пакет приходит от другого сервера: всё, что в нём текстом, резерв потом
пишет в конфиг `awg-quick` своей гостевой зоны. Поэтому хуки (`PostUp` и
соседи), `Table`, `DNS` и `I1–I5` не едут никогда, пир вне подсети
интерфейса отбрасывается, а чужая часть проверяется до сборки конфига.

Цена ошибки: `PostUp` из пакета в конфиге клона — команда основы,
выполненная root-ом резерва; пир с адресом вне подсети — чужой трафик через
зону резерва; негодная часть, прошедшая проверку, — клон, который не
поднимается в аварию, когда чинить уже поздно.
"""
from __future__ import annotations

import base64
import json

import pytest

from awgbot.util.rsvpkg import vpn

pytestmark = pytest.mark.unit


def _k(n: int) -> str:
    return base64.b64encode(bytes([n]) * 32).decode()


PRIV, P1, P2, P3, PSK1 = _k(1), _k(2), _k(3), _k(4), _k(5)

LIVE = f"""[Interface]
PrivateKey = {PRIV}
Address = 10.8.1.1/24
ListenPort = 51820
MTU = 1376
Table = off
DNS = 198.51.100.53
PostUp = iptables -t nat -A POSTROUTING -s 10.8.1.0/24 -j MASQUERADE
PostDown = iptables -t nat -D POSTROUTING -s 10.8.1.0/24 -j MASQUERADE
PreUp = /usr/local/sbin/awg-hook.sh
Jc = 4
Jmin = 10
Jmax = 50
S1 = 20
S2 = 30
S3 = 40
S4 = 50
H1 = 100-200
H2 = 500000100-500000900
H3 = 1000000100-1000090000
H4 = 1500000100-1999999999
I1 = <b 0xc0ffee>

[Peer]
PublicKey = {P1}
PresharedKey = {PSK1}
AllowedIPs = 10.8.1.2/32

[Peer]
PublicKey = {P2}
AllowedIPs = 10.8.1.3/32

[Peer]
# вне подсети интерфейса
PublicKey = {P3}
AllowedIPs = 10.9.0.5/32

[Peer]
PublicKey = not-a-key
AllowedIPs = 10.8.1.4/32
"""

_NEVER = ("PostUp", "PostDown", "PreUp", "PreDown", "Table", "DNS", "I1", "I2", "I3", "I4", "I5",
          "MASQUERADE", "awg-hook", "/usr/local")


def _part():
    return vpn.build([("awg0", LIVE)], admin=["10.8.1.2"], dns=["10.8.1.1"], upstreams=["198.51.100.53"],
                     tz="Asia/Yekaterinburg")


def test_the_part_carries_keys_parameters_and_peers_of_the_subnet():
    i = vpn.iface_part("awg0", LIVE)
    assert (i["name"], i["addr"], i["port"], i["priv"], i["mtu"]) == ("awg0", "10.8.1.1/24", 51820, PRIV, 1376)
    assert i["obf"]["Jc"] == "4" and i["obf"]["H4"] == "1500000100-1999999999"
    assert i["peers"] == [{"pub": P1, "psk": PSK1, "ip": "10.8.1.2"}, {"pub": P2, "psk": "", "ip": "10.8.1.3"}], \
        "пир вне подсети или с негодным ключом доехал в пакет"


def test_hooks_table_dns_and_i1_to_i5_never_get_into_the_part():
    text = json.dumps(_part(), ensure_ascii=False)
    leaked = [w for w in _NEVER if w in text]
    assert not leaked, f"в часть VPN попало: {leaked}"


def test_the_clone_conf_has_no_hooks_and_exactly_the_peers_of_the_part():
    part = vpn.check(_part())
    conf = vpn.clone_conf(part["ifaces"][0])
    leaked = [w for w in _NEVER if w in conf]
    assert not leaked, f"в конфиг клона попало: {leaked}\n{conf}"
    assert conf.count("[Peer]") == 2 and f"PublicKey = {P1}" in conf and f"PresharedKey = {PSK1}" in conf
    assert "AllowedIPs = 10.8.1.2/32" in conf and "AllowedIPs = 10.8.1.3/32" in conf
    assert "10.9.0.5" not in conf
    assert conf.startswith("[Interface]\nAddress = 10.8.1.1/24\nListenPort = 51820\n")


@pytest.mark.parametrize("broken, words", [
    (LIVE.replace(f"PrivateKey = {PRIV}", "PrivateKey = DUMMY"), "PrivateKey"),
    (LIVE.replace(f"PrivateKey = {PRIV}", f"PrivateKey = {PRIV[:-2]}x="), "PrivateKey"),
    (LIVE.replace("ListenPort = 51820\n", ""), "ListenPort"),
    (LIVE.replace("Address = 10.8.1.1/24", "Address = десять"), "адрес"),
])
def test_a_live_conf_that_does_not_fit_is_a_vpn_part_error(broken, words):
    with pytest.raises(vpn.VpnPartError, match=words):
        vpn.iface_part("awg0", broken)


# ── проверка чужой части на резерве ──────────────────────────────────────────

def _foreign(**iface_over):
    part = _part()
    part["ifaces"][0].update(iface_over)
    return part


def test_a_good_part_passes_the_check_as_is():
    part = _part()
    assert vpn.check(part) is part


@pytest.mark.parametrize("over", [
    {"peers": [{"pub": P1, "psk": "", "ip": "10.9.0.5"}]},          # пир вне подсети
    {"peers": [{"pub": "DUMMY", "psk": "", "ip": "10.8.1.2"}]},      # не ключ
    {"peers": [{"pub": P1, "psk": "DUMMY", "ip": "10.8.1.2"}]},      # не PSK
    {"obf": {"I1": "<b 0x00>"}},                                     # I1 — не едет
    {"obf": {"PostUp": "rm -rf /"}},                                 # не ключ протокола
    {"obf": "Jc = 4"},
    {"name": "../etc"}, {"name": "Awg0"}, {"name": "a"},
    {"port": 0}, {"port": 70000},
    {"priv": "DUMMY"},
    {"peers": "все"},
])
def test_a_foreign_part_that_does_not_fit_is_refused(over):
    with pytest.raises(vpn.VpnPartError):
        vpn.check(_foreign(**over))


@pytest.mark.parametrize("part", [{}, {"ifaces": []}, {"ifaces": "awg0"}, {"ifaces": ["awg0"]}, None, []])
def test_a_part_without_interfaces_is_refused(part):
    with pytest.raises(vpn.VpnPartError):
        vpn.check(part)


@pytest.mark.parametrize("value", ["4\nPostUp = touch /tmp/awgtst-pwned", "4\r\nTable = 100", "4 # x\nDNS = 1.1.1.1"])
def test_an_obfuscation_value_cannot_smuggle_a_line_into_the_clone_conf(value):
    """Значение Jc с переводом строки: проверка обязана его отвергнуть, иначе
    конфиг клона получает строку `PostUp`, и `awg-quick up` на резерве
    исполняет её от root."""
    part = _foreign(obf={"Jc": value})
    try:
        vpn.check(part)
    except vpn.VpnPartError:
        return
    conf = vpn.clone_conf(part["ifaces"][0])
    pytest.fail(f"значение обфускации с переводом строки прошло проверку; конфиг клона:\n{conf}")


@pytest.mark.parametrize("over", [{"addr": "не адрес"}, {"port": "много"}])
def test_garbage_in_typed_fields_is_a_vpn_part_error_not_a_bare_value_error(over):
    """Вызывающий ловит VpnPartError и пишет `pkg_bad` с причиной; голый
    ValueError из ipaddress/int уронил бы приём пакета."""
    with pytest.raises(vpn.VpnPartError):
        vpn.check(_foreign(**over))


@pytest.mark.parametrize("key, value", [("admin", ["не адрес"]), ("dns", ["10.8.1.1; rm"])])
def test_garbage_in_the_zone_lists_is_a_vpn_part_error(key, value):
    part = _part()
    part[key] = value
    with pytest.raises(vpn.VpnPartError):
        vpn.check(part)
