"""Часть VPN пакета переезда (util/rsvpkg/vpn.py): из живого конфига
интерфейса AWG сервера — только данные для клона, без хуков и путей; на
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


# ── пиры-зонды кворума и второй адрес интерфейса ─────────────────────────────
# Выпуск с кворумом кладёт на интерфейс пользователей пиры-зонды голосующих
# (169.254.88.0/24) и второй Address из того же блока; после отката они так и
# лежат в конфиге. Клон на резерве обязан встать с адресом подсети
# пользователей и без зондов: клон с адресом 169.254.88.1/26 — все
# пользователи без сети ровно в аварию.

Q4, Q5, Q6 = _k(40), _k(41), _k(42)
_PROBES = (f"\n[Peer]\nPublicKey = {Q4}\nAllowedIPs = 169.254.88.4/32\n"
           f"\n[Peer]\nPublicKey = {Q5}\nPresharedKey = {PSK1}\nAllowedIPs = 169.254.88.5/32\n"
           f"\n[Peer]\nPublicKey = {Q6}\nAllowedIPs = 169.254.88.6/32\n")
_PROBE_KEYS = {Q4, Q5, Q6}


def _with_probe_address(how: str) -> str:
    """LIVE с адресом блока зондов: первой строкой, второй строкой или через запятую."""
    if how == "first_line":
        live = LIVE.replace("Address = 10.8.1.1/24", "Address = 169.254.88.1/26\nAddress = 10.8.1.1/24")
    elif how == "second_line":
        live = LIVE.replace("Address = 10.8.1.1/24", "Address = 10.8.1.1/24\nAddress = 169.254.88.1/26")
    elif how == "comma_first":
        live = LIVE.replace("Address = 10.8.1.1/24", "Address = 169.254.88.65/26, 10.8.1.1/24")
    elif how == "comma_second":
        live = LIVE.replace("Address = 10.8.1.1/24", "Address = 10.8.1.1/24,169.254.88.65/26")
    else:
        raise AssertionError(how)
    assert live != LIVE
    return live + _PROBES


_HOWS = ["first_line", "second_line", "comma_first", "comma_second"]


@pytest.mark.parametrize("how", _HOWS)
def test_the_clone_address_is_always_from_the_users_subnet(how):
    i = vpn.iface_part("awg0", _with_probe_address(how))
    assert i["addr"] == "10.8.1.1/24", f"адрес клона взят из блока зондов ({how}): {i['addr']}"


@pytest.mark.parametrize("how", _HOWS)
def test_probe_peers_never_get_into_the_part(how):
    i = vpn.iface_part("awg0", _with_probe_address(how))
    got = {p["pub"] for p in i["peers"]}
    assert not got & _PROBE_KEYS, f"пиры-зонды доехали в часть VPN ({how})"
    assert not any(p["ip"].startswith("169.254.88.") for p in i["peers"])
    assert i["peers"] == [{"pub": P1, "psk": PSK1, "ip": "10.8.1.2"}, {"pub": P2, "psk": "", "ip": "10.8.1.3"}], \
        "пользователи интерфейса потерялись рядом с зондами"


@pytest.mark.parametrize("how", _HOWS)
def test_a_package_with_probes_in_the_live_conf_builds_seals_opens_and_checks(how):
    """Собранный пакет открывается и проходит проверку резерва (`check` тот
    же, что у выпусков без рельса), конфиг клона — без следа блока зондов."""
    from awgbot.util import rsvkeys
    from awgbot.util.rsvpkg import seal

    part = vpn.build([("awg0", _with_probe_address(how))], admin=["10.8.1.2"], dns=["10.8.1.1"],
                     upstreams=["198.51.100.53"], tz="Asia/Yekaterinburg")
    m, k = rsvkeys.new_master(), seal.new_k()
    blob = seal.seal({"vpn": part}, k=k, m=m, pkg_gen=1, m_gen=1, dns=None)
    opened = seal.open_parts(blob, k)["vpn"]
    checked = vpn.check(opened)
    conf = vpn.clone_conf(checked["ifaces"][0])
    # 4.2.0: пиры-зонды едут отдельным полем и ставятся на клон (его ответчик — следующий адрес
    # после второго адреса основы); адрес интерфейса клона — подсеть пользователей
    probes = checked["ifaces"][0].get("probes") or []
    assert {p["pub"] for p in probes} == _PROBE_KEYS, f"пиры-зонды не доехали полем probes ({how})"
    # линия a (.1) — клон на .2, линия b (.65, формы через запятую) — на .66
    assert checked["ifaces"][0]["probe_resp"] in ("169.254.88.2/26", "169.254.88.66/26"), checked["ifaces"][0].get("probe_resp")
    assert conf.startswith("[Interface]\nAddress = 10.8.1.1/24\n")
    assert conf.count("[Peer]") == 2 + len(probes)
    assert all(f"AllowedIPs = {p['ip']}/32" in conf for p in probes), "у зонда на клоне — ровно один /32 из блока"


def test_block_peers_are_ordinary_users_when_the_users_subnet_covers_the_block():
    """Подсеть пользователей 169.254.0.0/16 накрывает блок зондов — правило
    зондов не действует: пиры 169.254.88.x там — настоящие устройства, и без
    них клон поднялся бы в аварию без части пользователей. Адрес клона — из
    подсети пользователей, даже если строка блока стоит первой."""
    live = LIVE.replace("Address = 10.8.1.1/24", "Address = 169.254.88.1/26\nAddress = 169.254.0.1/16") \
        .replace("10.8.1.2/32", "169.254.1.2/32").replace("10.8.1.3/32", "169.254.1.3/32") + _PROBES
    i = vpn.iface_part("awg0", live)
    assert i["addr"] == "169.254.0.1/16"
    assert {p["ip"] for p in i["peers"]} == {"169.254.1.2", "169.254.1.3", "169.254.88.4", "169.254.88.5",
                                             "169.254.88.6"}, \
        f"устройства подсети пользователей, пересекающей блок, не доехали в часть VPN: {i['peers']}"


@pytest.mark.parametrize("address", [
    "Address = 169.254.88.1/24",
    "Address = 169.254.88.1/24\nAddress = 169.254.88.65/26",
    "Address = 169.254.88.1/24, 169.254.88.65/26 # кворум",
])
def test_an_interface_whose_every_address_is_in_the_block_takes_the_first_one(address):
    """Все адреса интерфейса в блоке — это подсеть пользователей, а не зонды:
    часть VPN строится на первом адресе, и устройства 169.254.88.x едут клону
    как обычные пользователи. Отказ сборки здесь оставил бы такой сервер без
    переезда вовсе."""
    live = LIVE.replace("Address = 10.8.1.1/24", address) \
        .replace("10.8.1.2/32", "169.254.88.2/32").replace("10.8.1.3/32", "169.254.88.3/32")
    i = vpn.iface_part("awg0", live)
    assert i["addr"] == "169.254.88.1/24", f"адрес клона не первый из Address: {i['addr']}"
    assert [p["ip"] for p in i["peers"]] == ["169.254.88.2", "169.254.88.3"], \
        f"устройства подсети 169.254.88.0/24 пропущены как зонды: {i['peers']}"
    conf = vpn.clone_conf(vpn.check(vpn.build([("awg0", live)], admin=["169.254.88.2"], dns=["169.254.88.1"],
                                              upstreams=["198.51.100.53"], tz="Asia/Yekaterinburg"))["ifaces"][0])
    assert conf.count("[Peer]") == 2 and "Address = 169.254.88.1/24\n" in conf


@pytest.mark.parametrize("address", [
    "Address = 10.8.1.1/24 # основной",
    "Address = 10.8.1.1/24\nAddress = 169.254.88.1/26 # кворум",
    "Address = 169.254.88.1/26 # кворум\nAddress = 10.8.1.1/24",
    "Address = 169.254.88.65/26 # кворум\nAddress = 10.8.1.1/24 # основной",
])
def test_a_comment_at_the_end_of_an_address_line_does_not_break_the_part(address):
    """4.2.0 подписывает свою строку Address комментарием: без его обрезки
    адрес не разбирается, и пакет переезда не собирается вообще."""
    i = vpn.iface_part("awg0", LIVE.replace("Address = 10.8.1.1/24", address) + _PROBES)
    assert i["addr"] == "10.8.1.1/24", f"адрес клона с комментарием в Address: {i['addr']!r}"
    assert {p["ip"] for p in i["peers"]} == {"10.8.1.2", "10.8.1.3"}


def test_a_block_peer_with_a_users_address_as_well_is_not_dropped_as_a_probe():
    """Пир «адрес блока + адрес пользователей» — не зонд (сверка поднимает на
    него тревогу); часть VPN обращается с ним как с любым пиром — по первому
    значению AllowedIPs и подсети интерфейса."""
    q7 = _k(43)
    live = LIVE + _PROBES + f"\n[Peer]\nPublicKey = {q7}\nAllowedIPs = 10.8.1.9/32, 169.254.88.9/32\n"
    i = vpn.iface_part("awg0", live)
    assert {"pub": q7, "psk": "", "ip": "10.8.1.9"} in i["peers"], f"пир пропущен как зонд: {i['peers']}"
    assert not {p["pub"] for p in i["peers"]} & _PROBE_KEYS
