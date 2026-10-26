"""
Конфиги WireGuard из канала (util/wgconf, install/gw-wg-allow.list): белый
список ключей и строгое правило AllowedIPs линка.

Конфиг линка, приехавший каналом, awg-quick исполнил бы вместе со строками
PreUp/PostUp от root, а AllowedIPs = 0.0.0.0/0 увёл бы весь трафик шлюза в
туннель. Поэтому принимается ровно то, что названо в списке данных, а записи
AllowedIPs — только те, что следуют из того же документа. Те же случаи гоняет
ядро проверки скрипта обвязки (wg_conf_check) — по тому же файлу списка.
"""
from __future__ import annotations

import pytest

from awgbot.util import wgconf

pytestmark = pytest.mark.unit

LINK = """# awg-bot: контракт линка 1
[Interface]
Address = 10.99.99.2/30
PrivateKey = DUMMY-LINK-PRIVATE-KEY=
Jc = 4
Jmin = 40
Jmax = 70
S1 = 10
S2 = 20
H1 = 1
H2 = 2
H3 = 3
H4 = 4

[Peer]
PublicKey = DUMMY-VPS-PUBLIC-KEY=
PresharedKey = DUMMY-LINK-PSK=
Endpoint = vps.example.org:51821
AllowedIPs = 10.8.1.0/24, 10.99.99.0/30
PersistentKeepalive = 25-35
"""
RULE = dict(client_subnet="10.8.1.0/24", link_cidr="10.99.99.0/30", peer_nets=[])


# ── список данных ────────────────────────────────────────────────────────────

def test_the_allow_list_ships_and_sets_the_limits():
    """Файл списка — в поставке; пределы правила заданы в нём, не в коде."""
    a = wgconf.allow()
    assert wgconf.ALLOW_LIST.name == "gw-wg-allow.list" and wgconf.ALLOW_LIST.is_file()
    assert a.allowed_max == 16 and a.allowed_min_prefix == 16
    assert [str(n) for n in a.allowed_nets] == ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


@pytest.mark.parametrize("kind,section,key", [
    ("link", "Interface", k) for k in ("PreUp", "PostUp", "PreDown", "PostDown", "SaveConfig", "DNS", "Table", "MTU")
] + [("link", "Peer", "PreUp"), ("uplink", "Interface", "PostUp"), ("uplink", "Interface", "DNS"),
     ("uplink", "Peer", "PostDown")])
def test_forbidden_keys_are_absent_from_the_list(kind, section, key):
    """PreUp/PostUp/PreDown/PostDown исполняются от root, DNS перепишет резолвер,
    Table у линка увёл бы трафик шлюза: ни одного из них в списке нет."""
    assert key not in wgconf.allow().keys[kind][section]


def test_without_the_list_nothing_is_accepted(monkeypatch, tmp_path):
    """Нет файла — закрыто по умолчанию, а не «принимаем всё»."""
    monkeypatch.setattr(wgconf, "_allow", None)
    monkeypatch.setattr(wgconf, "ALLOW_LIST", tmp_path / "нет.list")
    with pytest.raises(wgconf.WgConfError):
        wgconf.allow()


@pytest.mark.parametrize("text", ["link Interface\n", "allowed_max\n", "weird Interface Address\n",
                                  "link Interface Address\nlink Peer PublicKey\n"])
def test_a_broken_list_is_refused_not_half_read(text):
    with pytest.raises(wgconf.WgConfError):
        wgconf.Allow(text)


# ── белый список ключей ──────────────────────────────────────────────────────

def test_a_proper_link_conf_passes_and_keeps_its_stamp():
    conf = wgconf.parse(LINK)
    wgconf.check_keys(conf, "link")
    assert conf.stamp == "1" and conf.get("Peer", "Endpoint") == "vps.example.org:51821"


@pytest.mark.parametrize("extra", ["PostUp = /bin/sh -c id", "PreDown = echo", "DNS = 1.1.1.1", "Table = off",
                                   "SaveConfig = true", "MTU = 1280"])
def test_every_forbidden_interface_key_is_refused(extra):
    text = LINK.replace("Jc = 4\n", f"Jc = 4\n{extra}\n")
    with pytest.raises(wgconf.WgConfError, match="вне белого списка"):
        wgconf.check_keys(wgconf.parse(text), "link")


def test_a_second_peer_or_a_stray_comment_is_refused():
    with pytest.raises(wgconf.WgConfError, match="ровно"):
        wgconf.check_keys(wgconf.parse(LINK + "\n[Peer]\nPublicKey = x\n"), "link")
    with pytest.raises(wgconf.WgConfError, match="штамп"):
        wgconf.check_keys(wgconf.parse(LINK + "# ещё строка\n"), "link")


def test_keys_are_matched_case_insensitively_like_wg_does():
    conf = wgconf.parse(LINK.replace("AllowedIPs", "allowedips"))
    wgconf.check_keys(conf, "link")
    assert "AllowedIPs = 10.8.1.0/24, 10.99.99.0/30" in wgconf.canonical(conf, "link")


def test_canonical_text_ignores_spacing_order_and_stamp():
    """Отпечаток считается от канонического текста: пробелы, порядок ключей и
    штамп контракта его не меняют — иначе каждая правка руками рождала бы
    ложное расхождение."""
    a = wgconf.canonical(wgconf.parse(LINK), "link")
    shuffled = LINK.replace("# awg-bot: контракт линка 1\n", "").replace("Jc = 4\n", "").replace("H4 = 4\n", "H4 = 4\nJc   =   4\n")
    assert wgconf.canonical(wgconf.parse(shuffled), "link") == a
    assert "#" not in a and a.startswith("[Interface]\nAddress = 10.99.99.2/30\n")
    assert wgconf.render(wgconf.parse(LINK), "link", stamp="2").startswith("# awg-bot: контракт линка 2\n[Interface]")


@pytest.mark.parametrize("junk", ["[Weird]\nA = b\n", "[Interface]\nnoequals\n", "Address = 1\n", "[Interface]\nPre Up = x\n"])
def test_garbage_is_a_wgconf_error_not_a_crash(junk):
    with pytest.raises(wgconf.WgConfError):
        wgconf.parse(junk)


# ── правило AllowedIPs — те же случаи гоняет скрипт ──────────────────────────

ALLOWED_CASES = [
    # (записи, env-правило, допустимо)
    ("10.8.1.0/24, 10.99.99.0/30", RULE, True),
    ("10.8.1.0/24 10.99.99.0/30 192.168.5.0/24", {**RULE, "peer_nets": ["192.168.5.0/24"]}, True),
    ("10.8.1.0/24, 10.99.99.0/30, 10.9.1.1/32", {**RULE, "resolver": "10.9.1.1"}, True),
    ("0.0.0.0/0", RULE, False),                                          # всё в туннель
    ("10.8.1.0/24, 10.99.99.0/30, 10.0.0.0/8", {**RULE, "peer_nets": ["10.0.0.0/8"]}, False),   # шире /16
    ("10.8.1.0/24, 10.99.99.0/30, 172.16.0.0/12", {**RULE, "peer_nets": ["172.16.0.0/12"]}, False),
    ("10.8.1.0/24, 10.99.99.0/30, 8.8.8.0/24", {**RULE, "peer_nets": ["8.8.8.0/24"]}, False),   # публичная
    ("10.8.2.0/24, 10.99.99.0/30", RULE, False),                          # подсеть пользователей другая
    ("10.8.1.0/24, 10.99.99.4/30", RULE, False),                          # чужая /30
    ("10.8.1.0/24, 10.99.99.0/30, 192.168.7.0/24", RULE, False),          # запись не из документа
    ("10.8.1.0/24, 10.99.99.0/30, 10.9.1.2/32", {**RULE, "resolver": "10.9.1.1"}, False),   # /32 не резолвера
    ("10.8.1.0/24, 10.99.99.0/30", {**RULE, "lan_subnets": ["10.8.1.0/25"]}, False),        # пересекается с LAN
    ("10.8.1.0/24, 10.99.99.0/30", {**RULE, "iface_nets": ["10.99.99.0/24"]}, False),      # пересекается с интерфейсом
    ("10.99.99.0/30", RULE, False),                                       # нет подсети пользователей
    ("10.8.1.0/24", RULE, False),                                         # нет /30
    (", ".join(["10.8.1.0/24", "10.99.99.0/30"] + [f"192.168.{n}.0/24" for n in range(15)]),
     {**RULE, "peer_nets": [f"192.168.{n}.0/24" for n in range(15)]}, False),              # 17 записей
    ("10.8.1.0/24, 10.99.99.0/30, 192.168.5.1/24", {**RULE, "peer_nets": ["192.168.5.0/24"]}, False),  # не подсеть
]


@pytest.mark.parametrize("entries,rule,ok", ALLOWED_CASES)
def test_the_allowed_ips_rule_through_wgconf(entries, rule, ok):
    if ok:
        assert wgconf.check_allowed_ips(entries, **rule)
    else:
        with pytest.raises(wgconf.WgConfError):
            wgconf.check_allowed_ips(entries, **rule)


def test_the_wanted_set_is_what_the_server_will_put_in_the_config():
    """Сервер собирает AllowedIPs тем же правилом, что проверяет шлюз: набор по
    документу проходит проверку всегда."""
    want = wgconf.allowed_ips_want(client_subnet="10.8.1.0/24", link_cidr="10.99.99.0/30",
                                   peer_nets=["192.168.5.0/24"], resolver="10.9.1.1")
    assert want == ["10.8.1.0/24", "10.99.99.0/30", "192.168.5.0/24", "10.9.1.1/32"]
    assert wgconf.check_allowed_ips(want, client_subnet="10.8.1.0/24", link_cidr="10.99.99.0/30",
                                    peer_nets=["192.168.5.0/24"], resolver="10.9.1.1") == want


def test_pubkey_shape():
    assert wgconf.is_pubkey("A" * 43 + "=") and not wgconf.is_pubkey("A" * 42 + "=") and not wgconf.is_pubkey("")
