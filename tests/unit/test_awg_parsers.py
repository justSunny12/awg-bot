"""Unit: чистые парсеры awgbot.infra.awg — разбор awg0.conf / dump / валидация.

Docker не нужен: тестируем только текстовые парсеры (код сам помечает их как
«тестируются без контейнера»).
"""
import pytest

from awgbot.infra import awg

pytestmark = pytest.mark.unit

# 43 base64-символа + '=' — валидный WG-pubkey по формату _RE_PUBKEY.
PUB1 = "A" * 43 + "="
PUB2 = "B" * 43 + "="

_INTERFACE = """\
[Interface]
Address = 10.8.1.0/24
ListenPort = 43125
PrivateKey = SERVERPRIV
Jc = 4
Jmin = 40
Jmax = 70
S1 = 0
S2 = 0
# I1 = <b 0x...>
# I2 =
"""

_CONF = _INTERFACE + """\

[Peer]
PublicKey = {p1}
PresharedKey = PSK1
AllowedIPs = 10.8.1.1/32

[Peer]
PublicKey = {p2}
PresharedKey = PSK2
AllowedIPs = 10.8.1.2/32
""".format(p1=PUB1, p2=PUB2)


# ── _extract_param ───────────────────────────────────────────────────────────
def test_extract_param_plain_and_commented_and_missing():
    assert awg._extract_param(_INTERFACE, "Jc") == "4"
    assert awg._extract_param(_INTERFACE, "ListenPort") == "43125"
    assert awg._extract_param(_INTERFACE, "I1") == "<b 0x...>"   # закомментированный — берём значение
    assert awg._extract_param(_INTERFACE, "I2") == ""            # закомментированный пустой
    assert awg._extract_param(_INTERFACE, "Nonexistent") is None


def test_parse_interface_params():
    p = awg.parse_interface_params(_INTERFACE)
    assert p["Jc"] == "4" and p["Jmin"] == "40" and p["Jmax"] == "70"
    assert p["ListenPort"] == "43125"
    assert "S1" in p and "S2" in p


# ── parse_occupied_ips ───────────────────────────────────────────────────────
def test_parse_occupied_ips():
    assert awg.parse_occupied_ips(_CONF) == {"10.8.1.1", "10.8.1.2"}


def test_parse_occupied_ips_empty_conf():
    assert awg.parse_occupied_ips(_INTERFACE) == set()


# ── parse_dump ───────────────────────────────────────────────────────────────
def test_parse_dump_full_and_short():
    dump = "\t".join(["IFACE_PRIV", "IFACE_PUB", "43125", "off"]) + "\n"
    # полный: pub psk endpoint allowed handshake rx tx keepalive
    dump += "\t".join([PUB1, "PSK1", "1.2.3.4:51820", "10.8.1.1/32",
                       "1720000000", "1000", "2000", "25"]) + "\n"
    # краткий: не подключался — endpoint (none), нет handshake/rx/tx
    dump += "\t".join([PUB2, "PSK2", "(none)", "10.8.1.2/32", "0"]) + "\n"
    peers = awg.parse_dump(dump)
    assert len(peers) == 2
    a, b = peers
    assert a["public_key"] == PUB1 and a["address"] == "10.8.1.1"
    assert a["endpoint"] == "1.2.3.4:51820"
    assert a["last_handshake"] == 1720000000 and a["rx"] == 1000 and a["tx"] == 2000
    assert b["endpoint"] is None
    assert b["last_handshake"] is None and b["rx"] == 0 and b["tx"] == 0


def test_parse_dump_zero_handshake_is_none():
    dump = "IFACE\n" + "\t".join([PUB1, "PSK", "1.2.3.4:5", "10.8.1.1/32",
                                  "0", "0", "0", "25"]) + "\n"
    assert awg.parse_dump(dump)[0]["last_handshake"] is None


def test_parse_dump_skips_interface_line_and_blanks():
    assert awg.parse_dump("IFACE_ONLY\n\n") == []


# ── _split_conf / _build_conf (round-trip) ───────────────────────────────────
def test_split_conf_header_and_peers():
    header, peers = awg._split_conf(_CONF)
    assert header.startswith("[Interface]")
    assert "[Peer]" not in header
    assert [p["pubkey"] for p in peers] == [PUB1, PUB2]
    assert peers[0]["lines"][0] == "[Peer]"


def test_split_conf_no_peers():
    header, peers = awg._split_conf(_INTERFACE)
    assert peers == []
    assert header.startswith("[Interface]")


def test_build_conf_roundtrip_normalized():
    header, peers = awg._split_conf(_CONF)
    rebuilt = awg._build_conf(header, peers)
    # повторный разбор даёт те же пиры (нормализация пустых строк устойчива)
    h2, p2 = awg._split_conf(rebuilt)
    assert [p["pubkey"] for p in p2] == [PUB1, PUB2]
    assert rebuilt.endswith("\n") and "\n\n\n" not in rebuilt   # без тройных пустот


def test_build_conf_no_peers():
    assert awg._build_conf(_INTERFACE, []).endswith("\n")


def test_peer_block_shape():
    blk = awg._peer_block(PUB1, "PSKX", "10.8.1.9")
    assert blk["pubkey"] == PUB1
    assert blk["lines"][0] == "[Peer]"
    assert f"AllowedIPs = 10.8.1.9/32" in blk["lines"]


# ── валидаторы ───────────────────────────────────────────────────────────────
def test_validate_key():
    assert awg._validate_key(PUB1) == PUB1
    with pytest.raises(awg.AwgError):
        awg._validate_key("too-short")
    with pytest.raises(awg.AwgError):
        awg._validate_key("A" * 44)          # нет '=' в конце


def test_validate_ip():
    assert awg._validate_ip("10.8.1.5") == "10.8.1.5"
    for bad in ("999.1.1.1", "10.8.1", "10.8.1.1.1", "abc"):
        with pytest.raises(awg.AwgError):
            awg._validate_ip(bad)


# ── detect_topology: автодетект порта/подсети из живого контейнера ────────────
def _mock_docker(monkeypatch, *, rc=0, conf=""):
    import types
    from awgbot.infra import awg as _awg
    monkeypatch.setattr(_awg.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=rc, stdout=conf.encode()))


def test_detect_topology_reads_port_and_subnet(monkeypatch):
    _mock_docker(monkeypatch, conf=(
        "[Interface]\nPrivateKey = X\nAddress = 10.66.66.0/24\nListenPort = 51899\n\n"
        "[Peer]\nAllowedIPs = 10.66.66.7/32\n"))
    t = awg.detect_topology("amnezia-awg2")
    assert t == {"listen_port": 51899,
                 "subnet_prefix": "10.66.66",
                 "subnet_cidr": "10.66.66.0/24"}


def test_detect_topology_standard_values(monkeypatch):
    _mock_docker(monkeypatch, conf="[Interface]\nAddress = 10.8.1.0/24\nListenPort = 43125\n")
    t = awg.detect_topology("c")
    assert t["listen_port"] == 43125 and t["subnet_prefix"] == "10.8.1"


def test_detect_topology_dead_container_all_none(monkeypatch):
    _mock_docker(monkeypatch, rc=1)
    assert awg.detect_topology("x") == {
        "listen_port": None, "subnet_prefix": None, "subnet_cidr": None}


def test_detect_topology_missing_keys(monkeypatch):
    # конфиг есть, но без ListenPort/Address (бот ставится до полной настройки)
    _mock_docker(monkeypatch, conf="[Interface]\nPrivateKey = X\n")
    assert awg.detect_topology("c") == {
        "listen_port": None, "subnet_prefix": None, "subnet_cidr": None}


def test_detect_topology_address_with_v6_list(monkeypatch):
    # Address может быть списком "v4, v6" — берём первый (v4)
    _mock_docker(monkeypatch, conf=(
        "[Interface]\nAddress = 10.20.30.0/24, fd00::1/64\nListenPort = 40000\n"))
    t = awg.detect_topology("c")
    assert t["subnet_prefix"] == "10.20.30" and t["subnet_cidr"] == "10.20.30.0/24"


def test_detect_topology_docker_missing(monkeypatch):
    # docker не установлен → OSError внутри → мягкий None по всем полям
    from awgbot.infra import awg as _awg
    def _boom(*_a, **_k):
        raise OSError("docker not found")
    monkeypatch.setattr(_awg.subprocess, "run", _boom)
    assert awg.detect_topology("c") == {
        "listen_port": None, "subnet_prefix": None, "subnet_cidr": None}


# ── classify_vpn_link: различает client / full_access ────────────────────────
def test_classify_vpn_link_full_access(monkeypatch):
    from awgbot.domain import configgen
    fake = {"hostName": "1.2.3.4", "userName": "root",
            "containers": [{"awg": {"port": "43125", "subnet_address": "10.8.1.0"}}]}
    monkeypatch.setattr(configgen, "decode_vpn", lambda link: fake)
    info = configgen.classify_vpn_link("vpn://whatever")
    assert info["kind"] == "full_access" and info["host"] == "1.2.3.4"


def test_classify_vpn_link_client(monkeypatch):
    from awgbot.domain import configgen
    import json as _j
    lc = _j.dumps({"client_priv_key": "PRIV", "client_pub_key": "PUB", "client_ip": "10.8.1.7"})
    fake = {"containers": [{"awg": {"last_config": lc}}]}
    monkeypatch.setattr(configgen, "decode_vpn", lambda link: fake)
    info = configgen.classify_vpn_link("vpn://whatever")
    assert info["kind"] == "client" and info["client_priv_key"] == "PRIV"
