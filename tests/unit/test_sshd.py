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
    assert "nginx" in sshd.port_busy(2222)
    assert sshd.port_busy(2223) == ""


def test_out_of_range_port_is_refused_without_touching_anything(monkeypatch):
    called = []
    monkeypatch.setattr(sshd.subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(sshd.SshdError):
        sshd.set_port(70000)
    assert not called
