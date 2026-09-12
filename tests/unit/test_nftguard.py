"""nftguard — единственная точка файервола: рендер таблицы, сбор спецификации,
сверка по тику, снятие старых ворот, CLI-разбор."""
from __future__ import annotations

import json
import subprocess

import pytest

from awgbot.core import config, settings
from awgbot.infra import awg, nftguard


@pytest.fixture()
def host_mode(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "SUBNET_PREFIX", "10.9.1")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")
    monkeypatch.setattr(config, "SERVER_PORT", 42755)
    monkeypatch.setattr(config, "SSH_PORT", 2222)
    monkeypatch.setattr(nftguard, "listen_ports", lambda: [42755, 443])
    monkeypatch.setattr(nftguard, "_resolve", lambda h: {"home.example.org": ["203.0.113.9"]}.get(h, []))


def _conf(monkeypatch, **kw):
    vals = {"app.firewall.enabled": True, "app.firewall.ssh_allow": [],
            "app.firewall.open_tcp": [], "app.firewall.open_udp": [],
            "app.network.ssh_port": 2222}
    vals.update(kw)
    monkeypatch.setattr(settings, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(vals.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: bool(vals.get(k, d)))


# ── классификация вайтлиста ──────────────────────────────────────────────────

def test_classify_ip_cidr_and_hostname():
    assert nftguard.classify("203.0.113.7") == ("4", "203.0.113.7/32")
    assert nftguard.classify("198.51.100.0/24") == ("4", "198.51.100.0/24")
    assert nftguard.classify("2001:db8::/48") == ("6", "2001:db8::/48")
    assert nftguard.classify("Home.Example.ORG") == ("host", "home.example.org")
    for bad in ("", "999.1.1.1", "not a host", "10.0.0.0/33"):
        with pytest.raises(ValueError):
            nftguard.classify(bad)


def test_resolve_allow_keeps_last_answer_when_dns_is_down(monkeypatch):
    """Моргнувший резолвер не должен выкинуть админа из вайтлиста."""
    import socket
    nftguard._resolve_cache.clear()
    answers = [[("x", "y", "z", "", ("203.0.113.9", 0))], socket.gaierror("down")]

    def fake_gai(*a, **k):
        r = answers.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(socket, "getaddrinfo", fake_gai)
    assert nftguard.resolve_allow(["dyn.example.org"])[0] == ["203.0.113.9/32"]
    nftguard._resolve_cache["dyn.example.org"] = (0.0, ["203.0.113.9"])   # TTL истёк
    v4, v6, bad = nftguard.resolve_allow(["dyn.example.org"])
    assert v4 == ["203.0.113.9/32"] and bad == []


# ── спецификация и рендер ────────────────────────────────────────────────────

def test_spec_host_mode_per_peer(host_mode, monkeypatch):
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["203.0.113.7", "home.example.org"],
                          "app.firewall.open_tcp": [8443, "bad"]})
    spec = nftguard.build_spec(["10.9.1.5", "10.9.1.2", "10.9.1.5"])
    assert spec.per_peer and spec.own_forward
    assert spec.tunnel_nets4 == ["10.9.1.0/24"]
    assert spec.tunnel_admin4 == ["10.9.1.2", "10.9.1.5"]
    assert spec.ssh_allow4 == ["203.0.113.7/32", "203.0.113.9/32"]
    assert spec.open_tcp == [8443] and not spec.ssh_open
    assert spec.udp_ports == [42755, 443]


def test_spec_includes_migration_subnet(host_mode, monkeypatch):
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.10.1")
    _conf(monkeypatch)
    assert nftguard.build_spec([]).tunnel_nets4 == ["10.9.1.0/24", "10.10.1.0/24"]


def test_render_whitelist_mode_drops_ssh_before_auth(host_mode, monkeypatch):
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["203.0.113.7"]})
    text = nftguard.render(nftguard.build_spec(["10.9.1.5"]))
    lines = [ln.strip() for ln in text.splitlines()]
    assert lines[0] == "#!/usr/sbin/nft -f"
    # атомарная замена: объявить → удалить → создать
    assert "table inet awg_bot_guard" in lines and "delete table inet awg_bot_guard" in lines
    assert "type filter hook input priority filter; policy drop;" in lines
    assert "udp dport { 42755, 443 } accept" in lines
    assert "ip saddr @tunnel_nets4 udp dport 53 accept" in lines
    # SSH: устройства админа из туннеля → accept, остальной туннель → drop,
    # вайтлист → accept, все прочие → drop (до TCP-рукопожатия, не до auth)
    i_adm = lines.index("tcp dport 2222 ip saddr @ssh_tunnel4 accept")
    i_tun = lines.index("tcp dport 2222 ip saddr @tunnel_nets4 drop")
    i_wl = lines.index("tcp dport 2222 ip saddr @ssh_allow4 accept")
    i_drop = lines.index("tcp dport 2222 drop")
    assert i_adm < i_tun < i_wl < i_drop
    assert "tcp dport 2222 accept" not in lines
    assert "elements = { 203.0.113.7/32 }" in lines
    assert "elements = { 10.9.1.5 }" in lines
    assert "type filter hook forward priority filter; policy drop;" in lines
    assert "ip saddr @tunnel_nets4 accept" in lines and "ip daddr @tunnel_nets4 accept" in lines


def test_render_open_mode_when_whitelist_is_empty(host_mode, monkeypatch):
    _conf(monkeypatch)
    text = nftguard.render(nftguard.build_spec([]))
    assert "tcp dport 2222 accept" in text and "tcp dport 2222 drop\n" not in text
    assert "tcp dport 2222 ip saddr @tunnel_nets4 drop" in text, "туннель без админских устройств закрыт"
    assert "elements" not in text.split("set ssh_tunnel4")[1].split("}")[0]


def test_render_docker_mode_has_no_per_peer_and_no_forward(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    monkeypatch.setattr(config, "SERVER_PORT", 0)
    monkeypatch.setattr(nftguard, "listen_ports", lambda: [])
    monkeypatch.setattr(nftguard, "_docker_bridge_subnets", lambda: ["172.29.172.0/24"])
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["203.0.113.7"]})
    spec = nftguard.build_spec(["10.8.1.5"])
    assert not spec.per_peer and not spec.own_forward and spec.tunnel_admin4 == []
    text = nftguard.render(spec)
    assert "tcp dport 2222 ip saddr @tunnel_nets4 accept" in text
    assert "hook forward" not in text and "@ssh_tunnel4" not in text


def test_render_is_deterministic_and_has_no_timestamps(host_mode, monkeypatch):
    _conf(monkeypatch)
    a = nftguard.render(nftguard.build_spec(["10.9.1.5", "10.9.1.2"]))
    b = nftguard.render(nftguard.build_spec(["10.9.1.2", "10.9.1.5"]))
    assert a == b


def test_listen_ports_scan_confs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AWG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SERVER_PORT", 42755)
    (tmp_path / "awg1.conf").write_text("[Interface]\nListenPort = 42755\n", encoding="utf-8")
    (tmp_path / "awggw.conf").write_text("[Interface]\n  ListenPort=443\n", encoding="utf-8")
    (tmp_path / "junk.conf").write_text("[Interface]\nAddress = 1.1.1.1\n", encoding="utf-8")
    assert nftguard.listen_ports() == [42755, 443]


# ── сверка по тику ───────────────────────────────────────────────────────────

def _nft_stub(monkeypatch, *, table=True, live=("10.9.1.5",), calls=None):
    calls = calls if calls is not None else []

    def fake(args, timeout=10, check=True):
        calls.append(list(args))
        if args[:2] == ["-j", "list"]:
            if not table:
                return subprocess.CompletedProcess(args, 1, b"", b"no such table")
            doc = {"nftables": [{"metainfo": {}},
                                {"set": {"name": "ssh_tunnel4", "elem": list(live)}}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(doc).encode(), b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(nftguard, "_nft", fake)
    return calls


def test_reconcile_is_cheap_when_nothing_changed(host_mode, monkeypatch, tmp_path):
    _conf(monkeypatch)
    monkeypatch.setattr(nftguard, "RULES_FILE", str(tmp_path / "g.nft"))
    monkeypatch.setattr(nftguard, "RULES_DIR", str(tmp_path))
    spec = nftguard.build_spec(["10.9.1.5"])
    (tmp_path / "g.nft").write_text(nftguard.render(spec), encoding="utf-8")
    calls = _nft_stub(monkeypatch)
    assert nftguard.reconcile(["10.9.1.5"]) == "ok"
    assert calls == [["-j", "list", "set", "inet", "awg_bot_guard", "ssh_tunnel4"]], \
        "один exec на тик: только чтение set"


def test_reconcile_applies_when_text_differs(host_mode, monkeypatch, tmp_path):
    _conf(monkeypatch)
    monkeypatch.setattr(nftguard, "RULES_FILE", str(tmp_path / "g.nft"))
    monkeypatch.setattr(nftguard, "RULES_DIR", str(tmp_path))
    calls = _nft_stub(monkeypatch)
    assert nftguard.reconcile(["10.9.1.5"]) == "applied"
    assert (tmp_path / "g.nft").read_text(encoding="utf-8") == nftguard.render(nftguard.build_spec(["10.9.1.5"]))
    assert ["-c", "-f", calls[0][2]] == calls[0], "синтаксис проверяется до записи"
    assert calls[-1] == ["-f", str(tmp_path / "g.nft")]


def test_reconcile_restores_a_deleted_table(host_mode, monkeypatch, tmp_path):
    _conf(monkeypatch)
    monkeypatch.setattr(nftguard, "RULES_FILE", str(tmp_path / "g.nft"))
    monkeypatch.setattr(nftguard, "RULES_DIR", str(tmp_path))
    (tmp_path / "g.nft").write_text(nftguard.render(nftguard.build_spec(["10.9.1.5"])), encoding="utf-8")
    _nft_stub(monkeypatch, table=False)
    assert nftguard.reconcile(["10.9.1.5"]) == "restored"


def test_reconcile_resyncs_when_live_set_drifted(host_mode, monkeypatch, tmp_path):
    _conf(monkeypatch)
    monkeypatch.setattr(nftguard, "RULES_FILE", str(tmp_path / "g.nft"))
    monkeypatch.setattr(nftguard, "RULES_DIR", str(tmp_path))
    (tmp_path / "g.nft").write_text(nftguard.render(nftguard.build_spec(["10.9.1.5"])), encoding="utf-8")
    _nft_stub(monkeypatch, live=("10.9.1.5", "10.9.1.77"))
    assert nftguard.reconcile(["10.9.1.5"]) == "resynced"


def test_reconcile_does_nothing_when_disabled(monkeypatch):
    _conf(monkeypatch, **{"app.firewall.enabled": False})
    calls = _nft_stub(monkeypatch)
    assert nftguard.reconcile(["10.9.1.5"]) == "disabled" and calls == []


def test_live_set_parses_prefix_elements(monkeypatch):
    doc = {"nftables": [{"set": {"name": "x", "elem": [
        "10.9.1.5", {"prefix": {"addr": "203.0.113.0", "len": 24}},
        {"elem": {"val": "10.9.1.6", "expires": 1}}]}}]}
    monkeypatch.setattr(nftguard, "_nft", lambda a, **k: subprocess.CompletedProcess(a, 0, json.dumps(doc).encode(), b""))
    assert nftguard.live_set("x") == {"10.9.1.5", "203.0.113.0/24", "10.9.1.6"}


# ── старые ворота снимаются ──────────────────────────────────────────────────

def test_remove_legacy_ssh_gate_strips_postup_and_chain(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    conf = ("[Interface]\nAddress = 10.9.1.0/24\n"
            "PostUp = iptables -N AWGBOT_SSH 2>/dev/null || true; iptables -A AWGBOT_SSH -j DROP; true\n"
            "PostUp = iptables -A FORWARD -i awg1 -j ACCEPT\n\n[Peer]\nPublicKey = x\n")
    written = {}
    monkeypatch.setattr(awg, "read_file", lambda p: conf)
    monkeypatch.setattr(awg, "write_file", lambda p, c: written.__setitem__(p, c))
    monkeypatch.setattr(awg, "_backup_conf", lambda iface=None: None)
    calls = []

    def fake_exec(args, check=True, **kw):
        calls.append(args)
        # первый -D в INPUT снимает джамп, второй уже нечего
        if args[:2] == ["iptables", "-D"]:
            rc = 0 if calls.count(args) == 1 and args[2] == "INPUT" else 1
        elif args[:2] == ["iptables", "-F"]:
            rc = 0
        else:
            rc = 0
        return subprocess.CompletedProcess(args, rc, b"", b"")
    monkeypatch.setattr(awg, "_exec", fake_exec)
    assert awg.remove_legacy_ssh_gate() is True
    body = next(iter(written.values()))
    assert "AWGBOT_SSH" not in body
    assert "PostUp = iptables -A FORWARD -i awg1 -j ACCEPT" in body, "чужой PostUp цел"
    assert ["iptables", "-X", "AWGBOT_SSH"] in calls


def test_remove_legacy_ssh_gate_is_a_noop_when_clean(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(awg, "read_file", lambda p: "[Interface]\nAddress = 10.9.1.0/24\n")
    monkeypatch.setattr(awg, "write_file", lambda p, c: pytest.fail("нечего писать"))
    monkeypatch.setattr(awg, "_exec", lambda a, check=True, **k: subprocess.CompletedProcess(a, 1, b"", b""))
    assert awg.remove_legacy_ssh_gate() is False


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_cli_parse_entries_normalizes_and_dedupes():
    from tools import firewall as fw
    assert fw._parse_entries("203.0.113.7, 203.0.113.7 198.51.100.0/24 Dyn.Example.org") == \
        ["203.0.113.7/32", "198.51.100.0/24", "dyn.example.org"]
    with pytest.raises(ValueError):
        fw._parse_entries("203.0.113.7 nonsense!")


def test_cli_unknown_command_prints_help(capsys):
    from tools import firewall as fw
    assert fw.main(["bogus"]) == 2
    assert "setup" in capsys.readouterr().out


def test_cli_setup_offers_to_install_nftables_when_missing(monkeypatch, capsys):
    """Чистый хост без nft: мастер предлагает apt, а не падает; отказ — понятный выход."""
    import shutil
    from tools import firewall as fw
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/apt-get" if n == "apt-get" else None)
    monkeypatch.setattr(fw, "_yes", lambda prompt: False)
    assert fw.cmd_setup([]) == 1
    assert "apt install nftables" in capsys.readouterr().out
