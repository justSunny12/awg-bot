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
    monkeypatch.setattr(nftguard, "_tunnel_ifs", lambda: ["awg1"])
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
    i_adm = lines.index("tcp dport 2222 ip saddr @admin4 accept")
    i_tun = lines.index("tcp dport 2222 ip saddr @tunnel_nets4 drop")
    i_wl = lines.index("tcp dport 2222 ip saddr @ssh_allow4 accept")
    i_drop = lines.index("tcp dport 2222 drop")
    assert i_adm < i_tun < i_wl < i_drop
    assert "tcp dport 2222 accept" not in lines
    assert "elements = { 203.0.113.7/32 }" in lines
    assert "elements = { 10.9.1.5 }" in lines
    assert "type filter hook forward priority filter; policy drop;" in lines
    # пир → пир: устройствам админа можно, остальным клиентам друг до друга нет
    i_padm = lines.index('iifname { "awg1" } oifname { "awg1" } ip saddr @admin4 accept')
    i_pdrop = lines.index('iifname { "awg1" } oifname { "awg1" } drop')
    i_any = lines.index("ip saddr @tunnel_nets4 accept")
    assert i_padm < i_pdrop < i_any
    assert "ip daddr @tunnel_nets4 accept" in lines


def test_render_open_mode_when_whitelist_is_empty(host_mode, monkeypatch):
    _conf(monkeypatch)
    text = nftguard.render(nftguard.build_spec([]))
    assert "tcp dport 2222 accept" in text and "tcp dport 2222 drop\n" not in text
    assert "tcp dport 2222 ip saddr @tunnel_nets4 drop" in text, "туннель без админских устройств закрыт"
    assert "elements" not in text.split("set admin4")[1].split("}")[0]


def test_render_masquerades_clients_but_not_into_tunnels(host_mode, monkeypatch):
    """NAT клиентов живёт в этой же таблице. Исключения — awg-интерфейсы (пир
    к пиру) и линк до шлюза: шлюз маскарадит сам и должен видеть настоящий
    адрес клиента, иначе исключения из MASQUERADE на нём ни к чему применить."""
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["203.0.113.7"]})
    spec = nftguard.build_spec(["10.9.1.5"])
    assert spec.nat and spec.nat_exclude_ifs == ["awg1", "awglink"]
    lines = [ln.strip() for ln in nftguard.render(spec).splitlines()]
    assert "type nat hook postrouting priority srcnat; policy accept;" in lines
    assert 'oifname != { "awg1", "awglink" } ip saddr @tunnel_nets4 masquerade' in lines
    assert lines.index("type filter hook input priority filter; policy drop;") < \
        lines.index("type nat hook postrouting priority srcnat; policy accept;")


def test_remove_keeps_nat_on_host(host_mode, monkeypatch, tmp_path):
    """`firewall off` снимает фильтр, но не выход наружу для клиентов."""
    _conf(monkeypatch)
    monkeypatch.setattr(nftguard, "RULES_FILE", str(tmp_path / "g.nft"))
    monkeypatch.setattr(nftguard, "RULES_DIR", str(tmp_path))
    _nft_stub(monkeypatch)
    done = nftguard.remove()
    text = (tmp_path / "g.nft").read_text(encoding="utf-8")
    assert "masquerade" in text and "hook input" not in text
    assert any("NAT" in ln for ln in done)


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
    assert "hook forward" not in text and "@admin4" not in text and "iifname {" not in text
    assert "masquerade" not in text, "в докере NAT делает контейнер"


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
                                {"set": {"name": "admin4", "elem": list(live)}}]}
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
    assert calls == [["-j", "list", "set", "inet", "awg_bot_guard", "admin4"]], \
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


def test_reconcile_does_nothing_when_disabled_in_docker(monkeypatch):
    """Докерный режим: NAT клиентов — дело контейнера, таблицы нет вовсе."""
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    _conf(monkeypatch, **{"app.firewall.enabled": False})
    calls = _nft_stub(monkeypatch)
    assert nftguard.reconcile(["10.9.1.5"]) == "disabled" and calls == []


def test_reconcile_keeps_nat_when_firewall_is_off_on_host(host_mode, monkeypatch, tmp_path):
    """Выключенный файервол не означает «клиенты без интернета»: на хосте
    таблица остаётся в форме NAT-only. Раньше MASQUERADE давал контейнер или
    обвяз маршрутизации, и чистый хост оставался без выхода наружу."""
    _conf(monkeypatch, **{"app.firewall.enabled": False})
    monkeypatch.setattr(nftguard, "RULES_FILE", str(tmp_path / "g.nft"))
    monkeypatch.setattr(nftguard, "RULES_DIR", str(tmp_path))
    monkeypatch.setattr(nftguard, "ensure_persistence", lambda: [])
    _nft_stub(monkeypatch)
    assert nftguard.reconcile(["10.9.1.5"]) == "nat"
    text = (tmp_path / "g.nft").read_text(encoding="utf-8")
    assert "type nat hook postrouting priority srcnat" in text
    assert "ip saddr @tunnel_nets4 masquerade" in text
    assert "hook input" not in text and "@admin4" not in text, "фильтра без включения нет"
    assert nftguard.reconcile(["10.9.1.5"]) == "ok", "повтор ничего не переписывает"


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


def test_nat_covers_asks_the_live_table(host_mode, monkeypatch):
    """Доктор маршрутизации спрашивает «выйдет ли трафик наружу», а не «есть ли
    правило iptables»: NAT переехал в таблицу бота."""
    _conf(monkeypatch)
    table = {"nftables": [{"rule": {"expr": [{"masquerade": None}]}}]}
    sets = {"nftables": [{"set": {"name": "tunnel_nets4", "elem": [
        {"prefix": {"addr": "10.9.1.0", "len": 24}}]}}]}

    def fake(args, timeout=10, check=True):
        doc = table if args[:3] == ["-j", "list", "table"] else sets
        return subprocess.CompletedProcess(args, 0, json.dumps(doc).encode(), b"")
    monkeypatch.setattr(nftguard, "_nft", fake)
    assert nftguard.nat_covers("10.9.1.0/24")
    assert not nftguard.nat_covers("10.250.0.0/24"), "чужая подсеть не покрыта"
    monkeypatch.setattr(nftguard, "_nft",
                        lambda a, timeout=10, check=True: subprocess.CompletedProcess(a, 1, b"", b""))
    assert not nftguard.nat_covers("10.9.1.0/24"), "нет таблицы — нет NAT"


# ── таймер отката: единственная страховка от самозапирания ───────────────────

def test_arm_rollback_builds_a_systemd_timer_and_clears_the_previous(monkeypatch):
    """Форма команды — то, от чего зависит, случится ли откат вообще. Сломай
    её, и «правила применены с таймером» станет неправдой: узнать об этом
    можно, только заперев себе SSH."""
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    nftguard.arm_rollback(300, ["-p", "WorkingDirectory=/opt/awg-bot", "python", "-m",
                                "tools.firewall", "rollback"],
                          {"AWG_BOT_CONF_DIR": "/etc/awg-bot/conf"})
    # прежний таймер снимается ДО постановки нового, иначе их станет два
    stops = [c for c in calls if c[:2] == ["systemctl", "stop"]]
    assert stops, "прежний таймер не снят"
    arm = calls[-1]
    assert arm[0] == "systemd-run"
    assert f"--unit={nftguard.ROLLBACK_UNIT}" in arm
    assert "--on-active=300s" in arm and "--collect" in arm
    assert "--setenv=AWG_BOT_CONF_DIR=/etc/awg-bot/conf" in arm, \
        "окружение не доедет — откат сбросит флаг не в том конфиге"
    assert arm[-5:] == ["WorkingDirectory=/opt/awg-bot", "python", "-m", "tools.firewall", "rollback"]
    assert calls.index(stops[0]) < calls.index(arm)


def test_arm_rollback_reports_a_failed_timer(monkeypatch):
    """Не поставился таймер — это отказ, а не мелочь: правила уже применены."""
    def fake_run(argv, **kw):
        if argv and argv[0] == "systemd-run":
            return subprocess.CompletedProcess(argv, 1, b"", b"unit exists")
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(nftguard.GuardError):
        nftguard.arm_rollback(60, ["true"], {})


def test_rollback_armed_and_disarm_talk_to_the_timer_unit(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append(list(argv))
        rc = 0 if argv[:3] == ["systemctl", "is-active", "--quiet"] else 0
        return subprocess.CompletedProcess(argv, rc, b"", b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert nftguard.rollback_armed() is True
    assert seen[-1] == ["systemctl", "is-active", "--quiet", f"{nftguard.ROLLBACK_UNIT}.timer"]
    seen.clear()
    assert nftguard.disarm_rollback() is True
    units = {c[2] for c in seen if c[:2] == ["systemctl", "stop"]}
    assert units == {f"{nftguard.ROLLBACK_UNIT}.timer", f"{nftguard.ROLLBACK_UNIT}.service"}
    assert any(c[:2] == ["systemctl", "reset-failed"] for c in seen), \
        "упавший юнит останется в failed и помешает поставить таймер снова"


# ── персистентность: правила обязаны пережить перезагрузку ───────────────────

def test_ensure_persistence_adds_the_include_once(monkeypatch, tmp_path):
    main = tmp_path / "nftables.conf"
    monkeypatch.setattr(nftguard, "MAIN_CONF", str(main))
    monkeypatch.setattr(nftguard, "RULES_DIR", str(tmp_path / "nftables.d"))
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"", b""))
    done = nftguard.ensure_persistence()
    text = main.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/sbin/nft -f"), "пустой файл без шапки nft не исполнит"
    assert f'include "{tmp_path / "nftables.d"}/*.nft"' in text
    assert any("include" in d for d in done) and any("nftables.service" in d for d in done)
    # второй вызов ничего не дублирует
    nftguard.ensure_persistence()
    assert text.count("include") == main.read_text(encoding="utf-8").count("include")


def test_ensure_persistence_warns_instead_of_failing_when_enable_is_refused(monkeypatch, tmp_path):
    """Юнит не включился — говорим об этом, но не роняем применение: таблица
    уже в ядре, и падение здесь оставило бы вызывающего без ответа."""
    main = tmp_path / "nftables.conf"
    main.write_text('include "/etc/nftables.d/*.nft"\n', encoding="utf-8")
    monkeypatch.setattr(nftguard, "MAIN_CONF", str(main))
    monkeypatch.setattr(nftguard, "RULES_DIR", "/etc/nftables.d")
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"no unit"))
    done = nftguard.ensure_persistence()
    assert done and "ВНИМАНИЕ" in done[-1]


def test_ensure_persistence_raises_when_the_main_conf_is_unwritable(monkeypatch, tmp_path):
    monkeypatch.setattr(nftguard, "MAIN_CONF", str(tmp_path / "нет-каталога" / "nftables.conf"))
    monkeypatch.setattr(nftguard, "RULES_DIR", "/etc/nftables.d")
    with pytest.raises(nftguard.GuardError):
        nftguard.ensure_persistence()


# ── статус: словарь, которым живут и CLI, и экран ───────────────────────────

def test_status_reports_table_file_and_timer(host_mode, monkeypatch, tmp_path):
    _conf(monkeypatch, **{"app.firewall.ssh_allow": ["203.0.113.7"]})
    monkeypatch.setattr(nftguard, "RULES_FILE", str(tmp_path / "g.nft"))
    monkeypatch.setattr(nftguard, "RULES_DIR", str(tmp_path))
    (tmp_path / "g.nft").write_text(nftguard.render(nftguard.build_spec(["10.9.1.5"])),
                                    encoding="utf-8")
    _nft_stub(monkeypatch, live=("10.9.1.5",))
    monkeypatch.setattr(nftguard, "rollback_armed", lambda: True)
    monkeypatch.setattr(nftguard, "ufw_active", lambda: False)
    st = nftguard.status(["10.9.1.5"])
    assert st["enabled"] and st["present"] and st["file"] is True
    assert st["live_admin"] == {"10.9.1.5"} and st["rollback"] is True
    assert st["spec"].ssh_allow4 == ["203.0.113.7/32"] and st["ufw"] is False
    # файл разошёлся с желаемым — это видно
    (tmp_path / "g.nft").write_text("другое", encoding="utf-8")
    assert nftguard.status(["10.9.1.5"])["file"] is False


def test_nat_only_form_needs_no_chains_without_tunnel_nets(host_mode, monkeypatch):
    """Подсетей туннеля нет (конфиг ещё не заполнен) — маскарадить нечего, и
    таблица не должна содержать цепочку, которая пропускает всё подряд."""
    _conf(monkeypatch, **{"app.firewall.enabled": False})
    monkeypatch.setattr(nftguard, "tunnel_nets", lambda: [])
    text = nftguard.render(nftguard.build_spec([]))
    assert "masquerade" not in text and "hook postrouting" not in text
    assert "hook input" not in text


# ── доступ между подсетями за шлюзами (концепт «локальная сеть», функция B) ───────

def test_forward_between_links_opens_only_with_the_toggle_and_two_links(host_mode, monkeypatch):
    """Транзит линк ↔ линк — по тумблеру и только при двух линках; без наборов
    подсетей: что едет, ограничивает AllowedIPs на стороне шлюза, а таблица
    собирается и из CLI без БД. Стоит после invalid drop, до правил туннеля."""
    monkeypatch.setattr(nftguard, "link_ifaces", lambda: ["awglink", "awglink2"])
    _conf(monkeypatch, **{"app.firewall.enabled": True})
    text = nftguard.render(nftguard.build_spec(["10.9.1.5"]))
    assert 'iifname { "awglink", "awglink2" } oifname { "awglink", "awglink2" } accept' not in text, "тумблер выключен"
    _conf(monkeypatch, **{"app.firewall.enabled": True, "app.routing.peer_nets.enabled": True})
    text = nftguard.render(nftguard.build_spec(["10.9.1.5"]))
    lines = [ln.strip() for ln in text.splitlines()]
    rule = 'iifname { "awglink", "awglink2" } oifname { "awglink", "awglink2" } accept'
    assert rule in lines
    fwd = lines[lines.index("chain forward {"):]
    assert fwd.index("ct state invalid drop") < fwd.index(rule) < fwd.index("ip saddr @tunnel_nets4 accept")
    monkeypatch.setattr(nftguard, "link_ifaces", lambda: ["awglink"])
    assert "oifname { \"awglink\" } accept" not in nftguard.render(nftguard.build_spec(["10.9.1.5"])), "один линк — не с кем"


def test_nat_only_form_closes_links_when_the_toggle_is_off(host_mode, monkeypatch):
    """Файервол выключен, политика хоста открыта: единственное место, где
    выключенный тумблер может закрыть транзит линк ↔ линк — и обещание
    «закроется сразу» держится."""
    monkeypatch.setattr(nftguard, "link_ifaces", lambda: ["awglink", "awglink2"])
    _conf(monkeypatch, **{"app.firewall.enabled": False})
    text = nftguard.render(nftguard.build_spec(["10.9.1.5"]))
    assert 'iifname { "awglink", "awglink2" } oifname { "awglink", "awglink2" } drop' in text
    assert "policy accept" in text.split("chain forward", 1)[1], "политика хоста остаётся его"
    _conf(monkeypatch, **{"app.firewall.enabled": False, "app.routing.peer_nets.enabled": True})
    assert "chain forward" not in nftguard.render(nftguard.build_spec(["10.9.1.5"]))


# ── канал ВПС ↔ шлюз (концепт «канал линка»): одна строка и один набор ───────

def _links(monkeypatch, tmp_path, **confs):
    """Каталог конфигов awg: имя интерфейса → текст конфига линка."""
    monkeypatch.setattr(config, "AWG_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "")
    for name, text in confs.items():
        (tmp_path / f"{name}.conf").write_text(text, encoding="utf-8")


def test_link_peer_addresses_are_computed_from_the_link_configs(monkeypatch, tmp_path):
    """Адрес шлюза в /30 считается из конфига линка, а не спрашивается у ядра:
    таблицу собирает и CLI, и делает это до подъёма интерфейсов. Ошибись здесь —
    канал либо закрыт для своего шлюза, либо открыт лишнему адресу."""
    _links(monkeypatch, tmp_path,
           awglink="[Interface]\nTable = off\nAddress = 10.99.99.1/30\n",
           awglink2="[Interface]\nTable = off\nAddress = 10.99.99.5/30\n",
           awg1="[Interface]\nAddress = 10.9.1.1/24\n")          # клиентский — не линк
    assert nftguard.link_peer_addrs() == ["10.99.99.2", "10.99.99.6"]


@pytest.mark.parametrize("conf", [
    "[Interface]\nTable = off\nAddress = 10.99.99.1/24\n",        # не /30 — не линк слота
    "[Interface]\nTable = off\nPrivateKey = X==\n",               # без адреса
    "[Interface]\nTable = off\nAddress = 10.99.99.999/30\n",      # мусор вместо адреса
])
def test_a_link_config_without_a_usable_30_gives_no_peer(monkeypatch, tmp_path, conf):
    """Непонятный конфиг не должен породить выдуманный адрес: пустой набор
    закрывает канал, выдуманный адрес открывает порт кому попало."""
    _links(monkeypatch, tmp_path, awglink=conf)
    assert nftguard.link_peer_addrs() == []


def test_the_channel_rule_lets_in_the_gateway_and_nobody_else(host_mode, monkeypatch, tmp_path):
    """Единственное новое правило файервола: вход с адреса шлюза в его же /30 на
    порт канала. Клиентская подсеть в другом наборе — канал не для клиентов, и
    открывать его туннелю целиком значило бы отдать снимок любому устройству."""
    _links(monkeypatch, tmp_path, awglink="[Interface]\nTable = off\nAddress = 10.99.99.1/30\n")
    _conf(monkeypatch, **{"app.firewall.enabled": True, "app.routing.link_channel_port": 8787})
    spec = nftguard.build_spec(["10.9.1.5"])
    assert spec.link_peers4 == ["10.99.99.2"] and spec.link_channel_port == 8787
    lines = [ln.strip() for ln in nftguard.render(spec).splitlines()]
    rule = 'iifname { "awglink" } ip saddr @link_peers4 tcp dport 8787 accept'
    assert rule in lines, "вход канала — только из интерфейса линка: адрес /30 снаружи подделать можно"
    assert "elements = { 10.99.99.2 }" in lines
    inp = lines[lines.index("chain input {"):]
    inp = inp[:inp.index("}")]
    assert rule in inp, "правило вне входной цепочки"
    assert "type filter hook input priority filter; policy drop;" in inp, (
        "цепочка с policy drop — всё, что не разрешено строкой выше, не войдёт")
    peers_set = lines[lines.index("set link_peers4 {"):]
    assert "10.9.1.0/24" not in peers_set[:peers_set.index("}")], "клиентская подсеть в наборе канала"


def test_the_channel_port_follows_the_setting(host_mode, monkeypatch, tmp_path):
    """Порт слушателя и порт в правиле — один ключ настроек. Разойдись они, и
    канал молча не поднялся бы: шлюз стучится туда, где его дропают."""
    _links(monkeypatch, tmp_path, awglink="[Interface]\nTable = off\nAddress = 10.99.99.1/30\n")
    _conf(monkeypatch, **{"app.firewall.enabled": True, "app.routing.link_channel_port": 9099})
    assert 'iifname { "awglink" } ip saddr @link_peers4 tcp dport 9099 accept' in nftguard.render(
        nftguard.build_spec(["10.9.1.5"]))


def test_without_links_there_is_no_channel_rule_at_all(host_mode, monkeypatch, tmp_path):
    """Сервер без шлюзов канала не держит: лишняя открытая строка в таблице —
    поверхность, которой не за что платить."""
    _links(monkeypatch, tmp_path)
    _conf(monkeypatch, **{"app.firewall.enabled": True})
    spec = nftguard.build_spec(["10.9.1.5"])
    assert spec.link_peers4 == [] and spec.link_channel_port == 0
    assert "link_peers4 tcp dport" not in nftguard.render(spec)


def test_without_link_interfaces_the_channel_rule_is_not_widened(host_mode, monkeypatch, tmp_path):
    """Адреса шлюзов и порт известны, а имён интерфейсов линков нет (таблицу
    собрали, когда конфиг линка не прочитался по имени). Правило без iifname
    пустило бы на порт канала пакет с подделанным адресом /30 с любого
    интерфейса, включая публичный, — лучше не открыть канал вовсе."""
    import dataclasses
    _links(monkeypatch, tmp_path, awglink="[Interface]\nTable = off\nAddress = 10.99.99.1/30\n")
    _conf(monkeypatch, **{"app.firewall.enabled": True, "app.routing.link_channel_port": 8787})
    spec = nftguard.build_spec(["10.9.1.5"])
    assert spec.link_peers4 == ["10.99.99.2"] and spec.link_ifs, "сценарий собран не так"
    bare = dataclasses.replace(spec, link_ifs=[])
    text = nftguard.render(bare)
    assert "tcp dport 8787" not in text, "правило канала без интерфейсов линков открыто с любого интерфейса"
    assert "@link_peers4 tcp dport" not in text
