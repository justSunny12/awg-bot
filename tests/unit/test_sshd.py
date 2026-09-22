"""Порт sshd из чата (infra/sshd): правка конфигов, проверки до рестарта,
откат файлов при отказе. Команды подменены: ни ss, ни sshd, ни systemctl."""
from __future__ import annotations

import subprocess

import pytest

from awgbot.infra import sshd


# ── текст конфига ────────────────────────────────────────────────────────────

def test_primary_replaces_first_port_and_mutes_the_rest():
    src = "Include /etc/ssh/sshd_config.d/*.conf\nPort 22\nPort 2200\nPermitRootLogin no\n"
    out = sshd.rewrite_port(src, 2222, primary=True)
    lines = out.splitlines()
    assert lines[1] == "Port 2222"
    assert lines[2].startswith("# Port 2200") and "awg-bot" in lines[2]
    assert lines[3] == "PermitRootLogin no" and out.endswith("\n")
    assert sshd.rewrite_port(out, 2222, primary=True) == out, "повторный проход ничего не меняет"


def test_primary_uses_the_commented_default_line():
    """Дистрибутивный sshd_config держит `#Port 22` как документацию —
    раскомментировать её естественнее, чем плодить вторую строку."""
    src = "#Port 22\n#AddressFamily any\n"
    out = sshd.rewrite_port(src, 2222, primary=True)
    assert out == "Port 2222\n#AddressFamily any\n"


def test_primary_without_any_port_inserts_after_include():
    """Include в Debian стоит первым; наш Port должен идти ПОСЛЕ него, иначе
    drop-in с портом добавил бы второй слушающий порт."""
    src = "# comment\nInclude /etc/ssh/sshd_config.d/*.conf\nPermitRootLogin no\n"
    out = sshd.rewrite_port(src, 2222, primary=True)
    assert out.splitlines() == ["# comment", "Include /etc/ssh/sshd_config.d/*.conf",
                                "Port 2222", "PermitRootLogin no"]
    assert sshd.rewrite_port("", 2222, primary=True) == "Port 2222\n"


def test_dropin_only_mutes_ports():
    src = "  Port 2200\nX11Forwarding no\n"
    out = sshd.rewrite_port(src, 2222, primary=False)
    assert out.splitlines()[0].startswith("  # Port 2200") and "Port 2222" not in out
    assert sshd.rewrite_port("X11Forwarding no\n", 2222, primary=False) == "X11Forwarding no\n"


# ── набор команд ─────────────────────────────────────────────────────────────

class _Host:
    """Подмена subprocess.run: что отвечать на sshd -T / ss / systemctl."""

    def __init__(self, socket=False, ports=(2222,), listening=True, sshd_t_ok=True):
        self.calls: list[list[str]] = []
        self.socket, self.ports, self.listening, self.sshd_t_ok = socket, list(ports), listening, sshd_t_ok

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        out, rc = "", 0
        if args[:2] == ["sshd", "-T"]:
            out = "".join(f"port {p}\n" for p in self.ports) + "permitrootlogin no\n"
        elif args[:2] == ["sshd", "-t"]:
            rc = 0 if self.sshd_t_ok else 1
            return subprocess.CompletedProcess(args, rc, out, "" if rc == 0 else "Bad configuration option")
        elif args[0] == "ss":
            out = "LISTEN 0 128 0.0.0.0:2222 0.0.0.0:*\n" if self.listening else ""
        elif args[:2] == ["systemctl", "is-enabled"]:
            rc = 0 if self.socket else 1
        elif args[:2] == ["systemctl", "list-unit-files"]:
            out = "ssh.service enabled enabled\n" if args[-1] == "ssh.service" else ""
        return subprocess.CompletedProcess(args, rc, out, "")


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    main = tmp_path / "sshd_config"
    main.write_text("Include /etc/ssh/sshd_config.d/*.conf\nPort 22\n")
    dd = tmp_path / "sshd_config.d"
    dd.mkdir()
    (dd / "10-cloud.conf").write_text("Port 22\nPasswordAuthentication no\n")
    monkeypatch.setattr(sshd, "SSHD_CONFIG", str(main))
    monkeypatch.setattr(sshd, "SSHD_DROPIN_DIR", str(dd))
    monkeypatch.setattr(sshd, "SOCKET_DROPIN", str(tmp_path / "ssh.socket.d" / "awg-bot.conf"))
    monkeypatch.setattr(sshd.time, "sleep", lambda s: None)
    return main, dd / "10-cloud.conf", tmp_path / "ssh.socket.d" / "awg-bot.conf"


def test_set_port_rewrites_configs_and_restarts_the_service(cfg, monkeypatch, tmp_path):
    main, dropin, sock = cfg
    host = _Host()
    monkeypatch.setattr(sshd.subprocess, "run", host)
    lines = sshd.set_port(2222)
    assert main.read_text() == "Include /etc/ssh/sshd_config.d/*.conf\nPort 2222\n"
    assert dropin.read_text().startswith("# Port 22") and "PasswordAuthentication no" in dropin.read_text()
    assert not sock.exists(), "без socket-активации drop-in к ssh.socket не нужен"
    assert ["systemctl", "restart", "ssh.service"] in host.calls
    assert ["sshd", "-t"] in host.calls and host.calls.index(["sshd", "-t"]) < \
        host.calls.index(["systemctl", "restart", "ssh.service"]), "проверка ДО рестарта"
    assert any("слушает 2222" in l for l in lines)


def test_socket_activated_host_gets_a_socket_dropin(cfg, monkeypatch, tmp_path):
    main, _dropin, sock = cfg
    host = _Host(socket=True)
    monkeypatch.setattr(sshd.subprocess, "run", host)
    sshd.set_port(2222)
    assert sock.read_text() == "[Socket]\nListenStream=\nListenStream=2222\n"
    assert ["systemctl", "daemon-reload"] in host.calls
    assert ["systemctl", "restart", "ssh.socket"] in host.calls
    assert ["systemctl", "stop", "ssh.service"] in host.calls, "иначе старый sshd держит старый порт"


def test_refused_config_restores_files_and_does_not_restart(cfg, monkeypatch, tmp_path):
    main, dropin, _ = cfg
    before_main, before_dropin = main.read_text(), dropin.read_text()
    host = _Host(sshd_t_ok=False)
    monkeypatch.setattr(sshd.subprocess, "run", host)
    with pytest.raises(sshd.SshdError, match="sshd -t"):
        sshd.set_port(2222)
    assert main.read_text() == before_main and dropin.read_text() == before_dropin
    assert not any(c[:2] == ["systemctl", "restart"] for c in host.calls)


def test_unexpected_effective_ports_restore_files(cfg, monkeypatch, tmp_path):
    """`sshd -T` — истина: если после правки он называет не только наш порт
    (например, ListenAddress с портом в месте, которого мы не знаем), —
    не рестартуем и возвращаем файлы."""
    main, _, _ = cfg
    before = main.read_text()
    host = _Host(ports=(2222, 22))
    monkeypatch.setattr(sshd.subprocess, "run", host)
    with pytest.raises(sshd.SshdError, match="применил бы порты"):
        sshd.set_port(2222)
    assert main.read_text() == before
    assert not any(c[:2] == ["systemctl", "restart"] for c in host.calls)


def test_listen_address_with_port_is_refused_before_writing(cfg, monkeypatch, tmp_path):
    main, _, _ = cfg
    main.write_text("ListenAddress 0.0.0.0:22\n")
    host = _Host()
    monkeypatch.setattr(sshd.subprocess, "run", host)
    with pytest.raises(sshd.SshdError, match="ListenAddress"):
        sshd.set_port(2222)
    assert main.read_text() == "ListenAddress 0.0.0.0:22\n"
    assert not any(c[0] == "sshd" for c in host.calls)


def test_nobody_listening_after_restart_rolls_back_and_restarts_again(cfg, monkeypatch, tmp_path):
    main, _, _ = cfg
    before = main.read_text()
    host = _Host(listening=False)
    monkeypatch.setattr(sshd.subprocess, "run", host)
    with pytest.raises(sshd.SshdError, match="никто не слушает"):
        sshd.set_port(2222)
    assert main.read_text() == before
    assert sum(1 for c in host.calls if c[:2] == ["systemctl", "restart"]) == 2, \
        "второй рестарт — уже со старым конфигом"


def test_port_busy_reports_the_listener(monkeypatch):
    def run(args, **kw):
        out = "tcp LISTEN 0 128 0.0.0.0:2222 0.0.0.0:* users:((\"nginx\",pid=1,fd=6))\n" \
            if args[-1].endswith(":2222") else ""
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(sshd.subprocess, "run", run)
    assert sshd.port_busy(2222) == "nginx"
    assert sshd.port_busy(2223) == ""


def test_port_busy_without_process_name_is_still_busy(monkeypatch):
    """Не root или сокет ядра: `ss` не покажет users:(…) — порт всё равно занят."""
    monkeypatch.setattr(sshd.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(
        args, 0, "udp UNCONN 0 0 0.0.0.0:2222 0.0.0.0:*\n", ""))
    assert sshd.port_busy(2222) == "?"


def test_out_of_range_port_is_refused_without_touching_anything(monkeypatch):
    called = []
    monkeypatch.setattr(sshd.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(sshd.SshdError):
        sshd.set_port(70000)
    assert not called


# ── порт как факт и владелец конфига (шлюз) ──────────────────────────────────

_SS = ('LISTEN 0 128 0.0.0.0:2222 0.0.0.0:* users:(("sshd",pid=612,fd=3))\n'
       'LISTEN 0 128 [::]:2222 [::]:* users:(("sshd",pid=612,fd=4))\n'
       'LISTEN 0 4096 127.0.0.1:53 0.0.0.0:* users:(("dnsmasq",pid=700,fd=5))\n'
       'LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=800,fd=6))\n'
       # rpcbind.socket на OMV: держатели — rpcbind и systemd (pid 1); сервис
       # остановлен — останется один systemd
       'LISTEN 0 4096 0.0.0.0:111 0.0.0.0:* users:(("rpcbind",pid=765,fd=4),("systemd",pid=1,fd=146))\n'
       'LISTEN 0 4096 [::]:111 [::]:* users:(("systemd",pid=1,fd=148))\n'
       # чужой процесс с именем sshd под обычным пользователем
       'LISTEN 0 128 0.0.0.0:2200 0.0.0.0:* users:(("sshd",pid=9001,fd=3))\n')


def test_listening_ports_are_real_sshd_sockets_only(monkeypatch):
    """Имя процесса подделывается; сокеты socket-юнитов (rpcbind :111 на OMV)
    держит systemd. Порт sshd — только у сокета настоящего sshd (бинарь + uid 0)."""
    monkeypatch.setattr(sshd.subprocess, "run",
                        lambda args, **kw: subprocess.CompletedProcess(args, 0, _SS, ""))
    monkeypatch.setattr(sshd, "_is_sshd_pid", lambda pid: pid == 612)
    assert sshd.listening_ports() == [2222]


def test_is_sshd_pid_checks_exe_and_owner(monkeypatch):
    links = {"/proc/612/exe": "/usr/sbin/sshd", "/proc/9001/exe": "/home/u/sshd",
             "/proc/613/exe": "/usr/sbin/sshd (deleted)"}
    uids = {"/proc/612": 0, "/proc/9001": 1000, "/proc/613": 0}

    class St:
        def __init__(self, uid): self.st_uid = uid

    def readlink(p, **kw):
        if str(p) not in links:
            raise OSError(p)
        return links[str(p)]

    def stat(p, **kw):
        if str(p) not in uids:
            raise OSError(p)
        return St(uids[str(p)])
    monkeypatch.setattr(sshd.os, "readlink", readlink)
    monkeypatch.setattr(sshd.os, "stat", stat)
    assert sshd._is_sshd_pid(612) and sshd._is_sshd_pid(613)
    assert not sshd._is_sshd_pid(9001), "sshd с таким именем, но не root"
    assert not sshd._is_sshd_pid(4242), "процесса нет"


def test_listening_ports_empty_when_sshd_is_down(monkeypatch):
    calls = []

    def run(args, **kw):
        calls.append(args)
        if args[0] == "ss":
            return subprocess.CompletedProcess(args, 0, "\n".join(_SS.splitlines()[2:]) + "\n", "")
        return subprocess.CompletedProcess(args, 1, "", "")          # ssh.socket не включён
    monkeypatch.setattr(sshd.subprocess, "run", run)
    monkeypatch.setattr(sshd, "_is_sshd_pid", lambda pid: False)
    assert sshd.listening_ports() == [], "сокеты systemd без socket-активации sshd — не его порты"


def test_listening_ports_fall_back_to_config_under_socket_activation(monkeypatch):
    def run(args, **kw):
        if args[0] == "ss":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["systemctl", "is-enabled"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["sshd", "-T"]:
            return subprocess.CompletedProcess(args, 0, "port 2200\n", "")
        return subprocess.CompletedProcess(args, 1, "", "")
    monkeypatch.setattr(sshd.subprocess, "run", run)
    assert sshd.listening_ports() == [2200]


@pytest.fixture()
def owner_fs(tmp_path, monkeypatch):
    main = tmp_path / "sshd_config"
    dd = tmp_path / "sshd_config.d"
    dd.mkdir()
    monkeypatch.setattr(sshd, "SSHD_CONFIG", str(main))
    monkeypatch.setattr(sshd, "SSHD_DROPIN_DIR", str(dd))
    monkeypatch.setattr(sshd, "OMV_CONFIG", str(tmp_path / "omv-config.xml"))
    monkeypatch.setattr(sshd, "omv_ssh_conf", lambda: None)
    return main, dd, tmp_path / "omv-config.xml"


OMV_HEAD = ("# This file is auto-generated by openmediavault (https://www.openmediavault.org)\n"
            "# WARNING: Do not edit this file, your changes will get lost.\n\nProtocol 2\n"
            "HostKey /etc/ssh/ssh_host_rsa_key\nPort 22\n")


def test_owner_is_omv_by_header_and_its_database(owner_fs, monkeypatch):
    """Шапка OMV 7 — дословно с живых малин; порт владельца — из его базы."""
    main, _, omv = owner_fs
    main.write_text(OMV_HEAD)
    omv.write_text("<config/>")
    monkeypatch.setattr(sshd, "omv_ssh_conf", lambda: {"enable": True, "port": 22})
    o = sshd.owner()
    assert o and o.kind == "omv" and o.port == 22 and "Службы → SSH" in o.where
    assert "openmediavault" in o.detail
    # база не отвечает, шапка есть — всё равно OMV, порт неизвестен
    monkeypatch.setattr(sshd, "omv_ssh_conf", lambda: None)
    o = sshd.owner()
    assert o.kind == "omv" and o.port is None
    # шапка чужая, но база OMV отвечает про ssh — владелец OMV
    main.write_text("Port 22\n")
    monkeypatch.setattr(sshd, "omv_ssh_conf", lambda: {"enable": True, "port": 2222})
    assert sshd.owner().port == 2222


def test_plain_debian_and_cloud_init_have_no_owner(owner_fs):
    main, dd, _ = owner_fs
    main.write_text("# This is the sshd server system-wide configuration file.\n"
                    "Include /etc/ssh/sshd_config.d/*.conf\n#Port 22\n")
    (dd / "50-cloud-init.conf").write_text("# Managed by cloud-init\nPasswordAuthentication no\n")
    assert not sshd.owner()


def test_omv_header_without_omv_database_is_a_generator(owner_fs):
    """Файл с шапкой «do not edit», но без базы OMV: владелец — «иная
    программа», с найденной строкой в detail."""
    main, dd, _ = owner_fs
    main.write_text("Port 22\n")
    (dd / "10-ansible.conf").write_text("# Ansible managed: do not edit\nPort 2200\n")
    o = sshd.owner()
    assert o.kind == "generator" and "Ansible managed" in o.detail and o.files == [str(dd / "10-ansible.conf")]
    main.write_text(OMV_HEAD)
    assert sshd.owner().kind == "generator"
