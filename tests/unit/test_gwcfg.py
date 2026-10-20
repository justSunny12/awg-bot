"""
Документ конфигурации шлюза (domain/gwcfg): разделы, проверки значений,
отпечатки, сопоставление имён документа и юнита.

Документ едет каналом с ВПС на шлюз и применяется от root: каждое значение
проверяется строго, отвергается всё при одном непрошедшем; отпечатки — то,
по чему сервер решает «доставлять или нет», и ложное расхождение значило бы
бесконечную доставку, а ложное совпадение — молча не доехавшую настройку.
"""
from __future__ import annotations

import pytest

from awgbot.domain import gwcfg
from awgbot.domain.gwcfg import CfgError
from tests.unit.test_wgconf_allowed import LINK

pytestmark = pytest.mark.unit

ENV = {"CLIENT_SUBNET": "10.8.1.0/24", "ADMIN_IPS": "10.8.1.2 10.8.1.7", "LAN_MODE": "1",
       "LAN_SUBNETS": "192.168.68.0/24", "RESOLVER": "10.9.1.1", "PEER_LAN_NETS": "",
       "LINK_CHANNEL_PORT": "8787"}
MAIL = {"login": "box@example.org", "password": "DUMMY-pass", "imap_host": "imap.example.org",
        "imap_port": 993, "smtp_host": "smtp.example.org", "smtp_port": 465}


# ── имена и разделы ──────────────────────────────────────────────────────────

def test_document_names_are_the_unit_names():
    """С 3.3.1 таблицы сопоставления нет: ключи env документа — те же имена,
    что у строк юнита обвязки."""
    assert not hasattr(gwcfg, "unit_name") and not hasattr(gwcfg, "UNIT_NAMES")
    assert {"LAN_SUBNETS", "PEER_LAN_NETS"} <= set(gwcfg.ENV_KEYS)
    assert all(gwcfg.human(k) for k in (*gwcfg.ENV_KEYS, *gwcfg.SECTIONS)), "у раздела нет человеческого имени"


def test_the_document_carries_no_backup_phrase():
    """Своих копий у шлюза нет: ни раздела фразы, ни подсказки «куда уходят
    бэкапы» в почте, ни её проверок."""
    assert gwcfg.SECTIONS == ("server", "env", "link", "mail", "token", "uplink", "reserve")
    assert "backup" not in gwcfg.HUMAN
    for name in ("MIN_PHRASE", "MAIL_HINT", "check_backup", "backup_print", "backup_key_print"):
        assert not hasattr(gwcfg, name), name
    parsed = gwcfg.parse({"gen": 1, "hash": "ab" * 8, "env": ENV, "backup": {"phrase": "DUMMY phrase long"},
                          "mail": {**MAIL, "backup": "email"}})
    assert "backup" not in parsed["sections"] and "backup" not in parsed["sections"]["mail"]
    assert set(gwcfg.check_mail({**MAIL, "backup": "sms"})) == set(gwcfg.MAIL_KEYS)


# ── env ──────────────────────────────────────────────────────────────────────

def test_a_proper_env_passes_normalized():
    out = gwcfg.check_env({**ENV, "ADMIN_IPS": "  10.8.1.2   10.8.1.7 "})
    assert out["ADMIN_IPS"] == "10.8.1.2 10.8.1.7" and out["PEER_LAN_NETS"] == ""


@pytest.mark.parametrize("key,bad", [
    ("CLIENT_SUBNET", "8.8.8.0/24"), ("CLIENT_SUBNET", "10.0.0.0/8"), ("CLIENT_SUBNET", "10.8.1.0/29"),
    ("CLIENT_SUBNET", "10.8.1.1/24"), ("CLIENT_SUBNET", ""),
    ("ADMIN_IPS", "10.8.1.2 not-an-ip"), ("ADMIN_IPS", " ".join(f"10.8.1.{n}" for n in range(2, 67))),
    ("LAN_MODE", "yes"), ("RESOLVER", "dns.example.org"), ("LAN_SUBNETS", "192.168.68.0"),
    ("PEER_LAN_NETS", "192.168.5.0/31"), ("PEER_LAN_NETS", "8.8.8.0/24"),
    ("PEER_LAN_NETS", "192.168.5.0/24 192.168.5.0/25"), ("PEER_LAN_NETS", "192.168.5.1/24"),
    ("LINK_CHANNEL_PORT", "0"), ("LINK_CHANNEL_PORT", "70000"), ("LINK_CHANNEL_PORT", "порт"),
])
def test_each_env_value_is_checked_on_its_boundary(key, bad):
    with pytest.raises(CfgError):
        gwcfg.check_env({**ENV, key: bad})


def test_a_missing_required_key_refuses_the_whole_env():
    env = dict(ENV); del env["CLIENT_SUBNET"]
    with pytest.raises(CfgError, match="нет CLIENT_SUBNET"):
        gwcfg.check_env(env)


# ── прочие разделы ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [{**MAIL, "login": ""}, {**MAIL, "password": ""}, {**MAIL, "imap_host": "imap host"},
                                 {**MAIL, "smtp_port": 0}, {**MAIL, "imap_port": "x"}])
def test_mail_values_are_checked(bad):
    with pytest.raises(CfgError):
        gwcfg.check_mail(bad)


@pytest.mark.parametrize("token", ["", "abc", "12345:short", "12345:AAAAAAAAAAAAAAAAAAAAAAAAAAAA bad"])
def test_token_rules(token):
    with pytest.raises(CfgError):
        gwcfg.check_token({"value": token})


def test_token_and_server_name_accepted():
    assert gwcfg.check_token({"value": "123456789:AA-dummy-token-for-tests"})["value"].startswith("123456789:")
    assert gwcfg.check_server({"name": " awg-srv.example "})["name"] == "awg-srv.example"
    with pytest.raises(CfgError):
        gwcfg.check_server({"name": "a b"})


# ── конфиг линка ─────────────────────────────────────────────────────────────

def _fixed():
    from awgbot.util import wgconf
    conf = wgconf.parse(LINK)
    return {k: conf.get("Interface", k) for k in ("PrivateKey", "Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4")} | {
        "PublicKey": conf.get("Peer", "PublicKey"), "PresharedKey": conf.get("Peer", "PresharedKey")}


def test_the_link_conf_of_the_same_document_passes():
    conf = gwcfg.check_link(LINK, gwcfg.check_env(ENV), link_cidr="10.99.99.0/30", address="10.99.99.2/30", fixed=_fixed())
    assert conf.get("Peer", "AllowedIPs") == "10.8.1.0/24, 10.99.99.0/30"


@pytest.mark.parametrize("old,new,why", [
    ("Address = 10.99.99.2/30", "Address = 10.99.99.1/30", "адрес линка"),
    ("Address = 10.99.99.2/30", "Address = 10.99.98.2/30", "/30 линка"),
    ("PrivateKey = DUMMY-LINK-PRIVATE-KEY=", "PrivateKey = DUMMY-OTHER-PRIVATE-KEY=", "переходом"),
    ("PresharedKey = DUMMY-LINK-PSK=", "PresharedKey = DUMMY-OTHER-PSK=", "переходом"),
    ("Jc = 4", "Jc = 5", "переходом"),
    ("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30", "AllowedIPs = 0.0.0.0/0", "AllowedIPs"),
    ("Endpoint = vps.example.org:51821", "Endpoint = vps.example.org:0", "Endpoint"),
    ("PersistentKeepalive = 25-35", "PersistentKeepalive = many", "PersistentKeepalive"),
])
def test_link_conf_changes_outside_the_contract_are_refused(old, new, why):
    text = LINK.replace(old, new)
    assert text != LINK
    with pytest.raises(CfgError, match=why):
        gwcfg.check_link(text, gwcfg.check_env(ENV), link_cidr="10.99.99.0/30", address="10.99.99.2/30", fixed=_fixed())


def test_check_doc_refuses_everything_on_one_bad_value():
    parsed = gwcfg.parse({"gen": 3, "hash": "ab" * 8, "env": ENV, "server": {"name": "awg-srv"},
                          "mail": {**MAIL, "imap_port": 0}})
    with pytest.raises(CfgError, match="imap_port"):
        gwcfg.check_doc(parsed, link_cidr="10.99.99.0/30")
    out = gwcfg.check_doc(gwcfg.parse({"gen": 3, "hash": "ab" * 8, "env": ENV, "server": {"name": "awg-srv"}}),
                          link_cidr="10.99.99.0/30")
    assert set(out) == {"env", "server"}


def test_parse_keeps_only_known_sections_and_keys():
    """Данные с другой машины: неизвестный раздел или ключ пропускается, типы
    приводятся, длины ограничены."""
    parsed = gwcfg.parse({"gen": "7", "hash": "ZZ", "env": {**ENV, "EVIL": "x"}, "future": {"a": 1},
                          "link": {"conf": "x" * 100000, "extra": 1}, "token": {"value": 5}})
    assert parsed["gen"] == 7 and parsed["hash"] == "" and "future" not in parsed["sections"]
    assert "EVIL" not in parsed["sections"]["env"] and len(parsed["sections"]["link"]["conf"]) == 16 * 1024
    assert parsed["sections"]["token"] == {"value": "5"}


def test_build_skips_absent_sections_and_ack_round_trips():
    body = gwcfg.build(18, "9c41" * 4, {"env": ENV, "mail": None, "server": {"name": "s"}})
    assert set(body) == {"gen", "hash", "env", "server"}
    ack = gwcfg.ack_parse(gwcfg.ack_build(18, "9c41" * 4, applied=["link", "env"], wait={"mail": "ab12"},
                                          declined={"mail": "cd34"}, failed={"token": "другой бот"}, retry=True))
    assert ack["applied"] == ["env", "link"] and ack["wait"] == {"mail": "ab12"}
    assert ack["declined"] == {"mail": "cd34"} and ack["failed"] == {"token": "другой бот"} and ack["retry"]
    assert gwcfg.ack_parse({"failed": {"*": "<b>x</b>", "weird": "y"}, "wait": "no"})["failed"] == {"*": "<b>x</b>"}
    assert gwcfg.ack_parse({"declined": {"backup": "cd34"}})["declined"] == {}, "раздел прежних агентов принят"


# ── отпечатки ────────────────────────────────────────────────────────────────

def test_prints_are_stable_to_whitespace_and_order_and_differ_by_value():
    a = gwcfg.env_print(ENV)
    assert len(a) == gwcfg.PRINT_LEN
    assert gwcfg.env_print({**ENV, "ADMIN_IPS": " 10.8.1.2  10.8.1.7 "}) == a
    assert gwcfg.env_print({**ENV, "ADMIN_IPS": "10.8.1.7 10.8.1.2"}) != a, "порядок адресов — часть значения"
    assert gwcfg.link_print(LINK) == gwcfg.link_print(LINK.replace("# awg-bot: контракт линка 1\n", "").replace("Jc = 4", "Jc=4"))
    assert gwcfg.link_print(LINK) != gwcfg.link_print(LINK.replace("25-35", "25"))
    assert gwcfg.link_print("мусор") == ""


def test_mail_print_ignores_unknown_keys_and_is_empty_for_a_stub():
    assert gwcfg.mail_print(MAIL) == gwcfg.mail_print({**MAIL, "backup": "telegram"})
    assert gwcfg.mail_print({**MAIL, "password": "other"}) != gwcfg.mail_print(MAIL)
    assert gwcfg.mail_print(None) == "" and gwcfg.mail_print({**MAIL, "imap_host": ""}) == ""


def test_doc_print_changes_with_any_section():
    base = {"env": gwcfg.env_print(ENV), "server": gwcfg.server_print("s")}
    assert gwcfg.doc_print(base) != gwcfg.doc_print({**base, "server": gwcfg.server_print("t")})
    assert gwcfg.doc_print(base) == gwcfg.doc_print(dict(base))


# ── I1 линка (этап 3 «единых обновлений») ────────────────────────────────────

I1 = "<b 0xc70000000108ce1bc7e1d8a3b4a9><r 16><t><rc 8>"


def _with_i1(text: str, value: str = I1) -> str:
    return text.replace("H4 = 4\n", f"H4 = 4\nI1 = {value}\n")


def test_a_rekey_with_i1_passes_and_the_switch_takes_it():
    """Смена ключей с I1: проверка проходит, переключение берёт [Interface]
    нового целиком — I1 вместе с ключами."""
    import base64
    priv = base64.b64encode(b"DUMMY-new-link-private-key-32b!!").decode()   # DUMMY, 32 байта — ключ по форме
    pub = base64.b64encode(b"DUMMY-new-vps-public-key-32-byte").decode()
    new = _with_i1(LINK).replace("DUMMY-LINK-PRIVATE-KEY=", priv).replace("DUMMY-VPS-PUBLIC-KEY=", pub)
    conf = gwcfg.check_link_rekey(new, LINK)
    assert conf.get("Interface", "I1") == I1
    merged = gwcfg.merge_link_keys(LINK, conf)
    assert merged.get("Interface", "I1") == I1 and merged.get("Peer", "Endpoint") == "vps.example.org:51821"


def test_a_document_with_another_i1_is_refused_the_guard_is_not_weakened():
    fixed = _fixed() | {"I1": I1}
    standing = _with_i1(LINK)
    gwcfg.check_link(standing, gwcfg.check_env(ENV), link_cidr="10.99.99.0/30", address="10.99.99.2/30", fixed=fixed)
    for other in (_with_i1(LINK, "<b 0x01>"), LINK):          # другой I1 и I1 нет вовсе
        with pytest.raises(CfgError, match="переходом"):
            gwcfg.check_link(other, gwcfg.check_env(ENV), link_cidr="10.99.99.0/30", address="10.99.99.2/30",
                             fixed=fixed)
    with pytest.raises(CfgError, match="переходом"):              # и добавить I1 документ не вправе
        gwcfg.check_link(standing, gwcfg.check_env(ENV), link_cidr="10.99.99.0/30", address="10.99.99.2/30",
                         fixed=_fixed())


def test_the_document_link_section_carries_i1_from_the_issued_conf(services, monkeypatch, make_active_client):
    """Раздел link документа собирается из /root/gw-<линк>.conf — I1 едет с ним."""
    import builtins
    admin = make_active_client(name="Админ", tg_id=1, device_limit=0)
    dev = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(dev.device_id, "awglink", 51821, "10.99.99.0/30", slot_id=1)
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if str(path) == "/root/gw-awglink.conf":
            import io
            return io.StringIO(_with_i1(LINK))
        return real_open(path, *a, **kw)
    monkeypatch.setattr(builtins, "open", fake_open)
    text = services.gwcfg_link_conf(services.db.gateway(1), {"CLIENT_SUBNET": "10.8.1.0/24", "PEER_LAN_NETS": ""})
    from awgbot.util import wgconf
    assert text and wgconf.parse(text).get("Interface", "I1") == I1
