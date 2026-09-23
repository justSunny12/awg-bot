"""gwguard — сторона шлюза: чтение таблицы awg_gw_guard, локальные добавки,
дрейф списка устройств админа относительно бандла."""
from __future__ import annotations

import json
import subprocess

from awgbot.core import config
from awgbot.infra import gwguard


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out.encode(), stderr=b"")


def test_table_info_parses_sets_and_chains(monkeypatch):
    doc = {"nftables": [
        {"metainfo": {}},
        {"set": {"name": "tunnel_nets4", "elem": [{"prefix": {"addr": "10.9.1.0", "len": 24}},
                                                   {"prefix": {"addr": "10.99.99.0", "len": 30}}]}},
        {"set": {"name": "admin4", "elem": ["10.9.1.2", {"elem": {"val": "10.9.1.3"}}]}},
        {"chain": {"name": "input"}}, {"chain": {"name": "forward"}},
    ]}
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(0, json.dumps(doc)))
    info = gwguard.table_info()
    assert info["sets"]["tunnel_nets4"] == {"10.9.1.0/24", "10.99.99.0/30"}
    assert info["sets"]["admin4"] == {"10.9.1.2", "10.9.1.3"}
    assert info["chains"] == {"input", "forward"}
    assert info["masq_ifaces"] == set()
    assert info["ssh_ports"] == {}


def test_table_info_reads_the_ssh_ports_held_by_rules(monkeypatch):
    """Порт в правиле для сервера по линку и в переходе на ssh_in — то, что
    таблица держит на самом деле; агент сверяет его с портом sshd."""
    doc = {"nftables": [
        {"chain": {"name": "input"}}, {"chain": {"name": "tunnel_in"}}, {"chain": {"name": "ssh_in"}},
        {"rule": {"chain": "input", "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": 2222}},
            {"jump": {"target": "ssh_in"}}]}},
        {"rule": {"chain": "tunnel_in", "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "saddr"}}, "right": "10.99.99.1"}},
            {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": 2222}},
            {"accept": None}]}},
    ]}
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(0, json.dumps(doc)))
    assert gwguard.table_info()["ssh_ports"] == {"input": 2222, "tunnel_in": 2222}


def test_table_info_lists_masqueraded_interfaces(monkeypatch):
    doc = {"nftables": [
        {"chain": {"name": "postrouting"}},
        {"rule": {"chain": "postrouting", "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "saddr"}}, "right": "@tunnel_nets4"}},
            {"match": {"op": "==", "left": {"meta": {"key": "oifname"}}, "right": "end0"}},
            {"masquerade": None}]}},
        {"rule": {"chain": "postrouting", "expr": [
            {"match": {"op": "==", "left": {"meta": {"key": "oifname"}}, "right": "awg0"}},
            {"masquerade": None}]}},
        {"rule": {"chain": "forward", "expr": [
            {"match": {"op": "==", "left": {"meta": {"key": "oifname"}}, "right": "awglink"}},
            {"accept": None}]}},
    ]}
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(0, json.dumps(doc)))
    assert gwguard.table_info()["masq_ifaces"] == {"end0", "awg0"}


def test_table_info_none_when_absent(monkeypatch):
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(1))
    assert gwguard.table_info() is None


def test_extra_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(gwguard, "FW_ENV", str(tmp_path / "firewall.env"))
    assert gwguard.read_extra() == []
    gwguard.write_extra(["10.9.1.7", "192.168.1.0/24"])
    assert gwguard.read_extra() == ["10.9.1.7", "192.168.1.0/24"]
    text = (tmp_path / "firewall.env").read_text(encoding="utf-8")
    assert 'ADMIN_IPS_EXTRA="10.9.1.7 192.168.1.0/24"' in text, "формат sh-переменной для юнита"
    import pytest
    with pytest.raises(ValueError):
        gwguard.write_extra(["not-an-ip"])


def test_unit_admin_ips_reads_the_bundle_value(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GW_UNIT", "awg-link-gw.service")
    unit = tmp_path / "awg-link-gw.service"
    unit.write_text('[Service]\nEnvironment=LINK_IF=awglink\nEnvironment="ADMIN_IPS=10.9.1.2 10.9.1.3"\n',
                    encoding="utf-8")
    import pathlib
    real = pathlib.Path.read_text
    monkeypatch.setattr(pathlib.Path, "read_text",
                        lambda self, *a, **k: real(unit, *a, **k) if str(self).endswith("awg-link-gw.service") else real(self, *a, **k))
    assert gwguard.unit_admin_ips() == ["10.9.1.2", "10.9.1.3"]


def test_forward_policy_from_foreign_chain(monkeypatch):
    doc = {"nftables": [{"chain": {"name": "FORWARD", "policy": "drop"}}]}
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(0, json.dumps(doc)))
    assert gwguard.iptables_forward_policy() == "drop"
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(1))
    assert gwguard.iptables_forward_policy() is None


# ── ВПС: напоминание о перевыпуске при смене устройств админа ────────────────

def test_gw_bundle_drift_notifies_once_per_change(services, make_active_client, monkeypatch):
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    assert services.gw_bundle_drift_notes() == [], "бандл ещё не собирали — молчим"
    services.db.set_state(services._GW_BUNDLE_SSH_KEY + "_1", "")   # бандл слота 1 собран с пустым списком
    services.add_device(admin.id, "phone")
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "Перевыпусти" in notes[0].text
    assert services.gw_bundle_drift_notes() == [], "то же расхождение второй раз не шлём"
    services.add_device(admin.id, "laptop")
    assert len(services.gw_bundle_drift_notes()) == 1, "новое расхождение — новое напоминание"


# ── firewall.env целиком: порт, фильтр, адреса снаружи ───────────────────────

def test_env_keeps_unknown_lines_and_validates_values(tmp_path, monkeypatch):
    import pytest
    p = tmp_path / "firewall.env"
    monkeypatch.setattr(gwguard, "FW_ENV", str(p))
    p.write_text('# старый файл\nADMIN_IPS_EXTRA="10.9.1.7"\nFOO="bar"\n', encoding="utf-8")
    gwguard.write_env(SSH_PORT="2222", SSH_FILTER="1",
                      SSH_ALLOW="home2.dyn.example 203.0.113.7/32", SSH_ALLOW_RESOLVED="198.51.100.4")
    env = gwguard.read_env()
    assert env["ADMIN_IPS_EXTRA"] == "10.9.1.7" and env["FOO"] == "bar", "чужое сохраняется"
    assert env["SSH_PORT"] == "2222" and env["SSH_FILTER"] == "1"
    assert env["SSH_ALLOW"] == "home2.dyn.example 203.0.113.7/32"
    text = p.read_text(encoding="utf-8")
    assert 'SSH_PORT="2222"' in text and text.startswith("# awg-bot"), "формат sh для юнита"
    assert gwguard.read_extra() == ["10.9.1.7"]
    for bad in ({"SSH_PORT": "70000"}, {"SSH_PORT": "22a"}, {"SSH_ALLOW": "2001:db8::1 ok; rm -rf /"},
                {"SSH_ALLOW_RESOLVED": "home.example.org"}, {"NOPE": "1"},
                # файл исполняется sh: scope id v6 и «имена» с ведущей цифрой не проходят
                {"ADMIN_IPS_EXTRA": "fe80::1%$(id)"}, {"SSH_ALLOW": "12345"}, {"SSH_ALLOW": "1.2.3.4.5"}):
        with pytest.raises(ValueError):
            gwguard.write_env(**bad)
    assert gwguard.read_env()["SSH_PORT"] == "2222", "отказ ничего не пишет"
    gwguard.write_env(SSH_FILTER="0", SSH_PORT="")
    assert gwguard.read_env()["SSH_FILTER"] == "0" and gwguard.read_env()["SSH_PORT"] == ""


def test_set_sync_writes_one_transaction_only_on_drift(monkeypatch, tmp_path):
    calls = []
    written = []

    def nft(args, timeout=10):
        calls.append(list(args))
        if args[0] == "-f":
            written.append(open(args[1]).read())
        return _cp(0)
    monkeypatch.setattr(gwguard, "_nft", nft)
    info = {"sets": {"ssh_allow4": {"203.0.113.7", "198.51.100.0/24"}, "server4": set()}, "chains": set()}
    assert gwguard.set_sync("ssh_allow4", {"203.0.113.7/32", "198.51.100.0/24"}, info) is False
    assert not calls, "совпало — ни одного exec"
    assert gwguard.set_sync("ssh_allow4", {"203.0.113.7", "198.51.100.4"}, info) is True
    assert written[-1] == ("flush set inet awg_gw_guard ssh_allow4\n"
                           "add element inet awg_gw_guard ssh_allow4 { 198.51.100.4, 203.0.113.7 }\n")
    assert gwguard.set_sync("server4", set(), info) is False
    # пересечения схлопываются до записи: nft отвергает «conflicting intervals»
    assert gwguard.set_sync("ssh_allow4", {"203.0.113.0/24", "203.0.113.7", "203.0.112.0/24"}, info) is True
    assert written[-1].splitlines()[1].endswith("{ 203.0.112.0/23 }")
    assert gwguard.set_sync("server4", {"198.51.100.10"}, info) is True
    assert gwguard.set_sync("nope", {"1.2.3.4"}, info) is False, "старая обвязка без набора — молча"
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(1))
    import pytest
    with pytest.raises(gwguard.GwGuardError):
        gwguard.set_sync("server4", {"198.51.100.11"}, info)


def test_server_host_prefers_the_live_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(gwguard, "_endpoint_host", lambda iface: "203.0.113.10")
    assert gwguard.server_host() == "203.0.113.10"
    monkeypatch.setattr(gwguard, "_endpoint_host", lambda iface: "")
    conf = tmp_path / "awglink.conf"
    conf.write_text("[Peer]\nPublicKey = x\nEndpoint = vpn.example.org:51820\n")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    assert gwguard.server_host() == "vpn.example.org"
    monkeypatch.setattr(config, "GW_LINK_CONF", str(tmp_path / "none.conf"))
    assert gwguard.server_host() == ""


def test_collapse_and_overlaps_and_lan_nets(monkeypatch):
    assert gwguard.collapse(["203.0.113.0/24", "203.0.113.7", "home.example.org", "198.51.100.4"]) \
        == ["198.51.100.4", "203.0.113.0/24"]
    assert gwguard.overlaps("203.0.113.7", ["198.51.100.0/24", "203.0.113.0/24"]) == "203.0.113.0/24"
    assert gwguard.overlaps("203.0.113.0/24", ["203.0.113.7"]) == "203.0.113.7", "и подсеть поверх адреса"
    assert gwguard.overlaps("203.0.113.7", ["home.example.org"]) == ""
    routes = {("route", "show", "default"): [{"dst": "default", "dev": "end0"}],
              ("route", "show", "dev", "end0", "scope", "link"): [
                  {"dst": "192.168.1.0/24"}, {"dst": "192.168.1.0/24"}, {"dst": "10.42.0.0/16"}]}
    monkeypatch.setattr(gwguard, "_ip_json", lambda a: routes.get(tuple(a), []))
    assert gwguard.lan_nets() == ["192.168.1.0/24", "10.42.0.0/16"]
    assert gwguard.lan_nets("wlan0") == []


# ── статистика апстримов dnsmasq (servers.bind) ─────────────────────────────

def _dig(monkeypatch, *, out="", rc=0, exc=None):
    """subprocess.run для dig: что вернул и с какими аргументами звали."""
    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(list(argv))
        if exc is not None:
            raise exc
        return _cp(rc, out)
    monkeypatch.setattr(gwguard.subprocess, "run", run)
    return calls


def test_upstream_stats_reads_sent_and_failed_per_server(monkeypatch):
    """`dig servers.bind CH TXT` у dnsmasq — «отправлено / отказов» по каждому
    апстриму. Запрос идёт в 127.0.0.1 классом CHAOS и наружу не уходит."""
    calls = _dig(monkeypatch, out='"10.9.1.1#53 1234 5"\n"1.1.1.1#53@awg0 7 7"\n')
    assert gwguard.upstream_stats() == {"10.9.1.1#53": (1234, 5), "1.1.1.1#53@awg0": (7, 7)}
    argv = calls[0]
    assert argv[0] == "dig" and "@127.0.0.1" in argv and argv[-3:] == ["servers.bind", "CH", "TXT"], (
        f"спросили не dnsmasq и не статистику: {argv}")


def test_upstream_stats_on_an_empty_answer_is_an_empty_dict(monkeypatch):
    _dig(monkeypatch, out="")
    assert gwguard.upstream_stats() == {}
    _dig(monkeypatch, out=";; connection timed out; no servers could be reached\n")
    assert gwguard.upstream_stats() == {}, "текст ошибки dig разобран как статистика"


def test_upstream_stats_is_none_when_dig_cannot_answer(monkeypatch):
    """dig нет, завис или вернул ошибку — «нечем проверить», а не пустая
    статистика и не исключение посреди тика монитора."""
    _dig(monkeypatch, exc=FileNotFoundError("dig"))
    assert gwguard.upstream_stats() is None
    _dig(monkeypatch, exc=subprocess.TimeoutExpired(["dig"], 15))
    assert gwguard.upstream_stats() is None
    _dig(monkeypatch, rc=9, out='"10.9.1.1#53 1 0"\n')
    assert gwguard.upstream_stats() is None


# ── наши ACCEPT в чужом ip filter FORWARD ────────────────────────────────────

def test_forward_accepts_lists_our_interface_rules(monkeypatch):
    """Раздел 3a скрипта открывает транзит своим интерфейсам в цепочке, где
    docker поставил DROP. Правила с условиями — чужие, в счёт не идут."""
    out = ("-P FORWARD DROP\n"
           "-A FORWARD -o awglink -j ACCEPT\n"
           "-A FORWARD -i awglink -j ACCEPT\n"
           "-A FORWARD -i awg0 -j ACCEPT\n"
           "-A FORWARD -j DOCKER-USER\n"
           "-A FORWARD -i docker0 ! -o docker0 -j ACCEPT\n"
           "-A FORWARD -o eth0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\n")
    calls = _dig(monkeypatch, out=out)
    assert gwguard.forward_accepts() == {"o:awglink", "i:awglink", "i:awg0"}
    assert calls[0][:1] == ["iptables"] and "-S" in calls[0] and "FORWARD" in calls[0]


def test_forward_accepts_without_iptables_is_empty(monkeypatch):
    _dig(monkeypatch, exc=FileNotFoundError("iptables"))
    assert gwguard.forward_accepts() == set()
    _dig(monkeypatch, exc=subprocess.TimeoutExpired(["iptables"], 10))
    assert gwguard.forward_accepts() == set()
