"""
install/awg-resolver-setup.sh — резолвер клиентов (dnsmasq на <подсеть>.1):
прогоном с подставными systemctl/apt-get, а не чтением исходника. Скрипт
переписывает конфиг демона, от которого у клиентов с приватным DNS зависит
весь резолв, — ошибка здесь оставляет людей без интернета.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install" / "awg-resolver-setup.sh"


@pytest.fixture()
def host(tmp_path):
    """Подставной хост: systemctl/apt-get пишут в журнал; sed понимает `-i` по-GNU."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    journal = tmp_path / "journal"
    (bin_dir / "systemctl").write_text(
        '#!/bin/sh\necho "systemctl $*" >> "$JOURNAL"\n'
        'case "$1" in list-unit-files) cat "$UNITS" 2>/dev/null ;; is-active) [ -f "$ACTIVE" ] ;; esac\n',
        encoding="utf-8")
    (bin_dir / "apt-get").write_text(
        '#!/bin/sh\necho "apt-get $*" >> "$JOURNAL"\necho "dnsmasq.service enabled" >> "$UNITS"\n',
        encoding="utf-8")
    (bin_dir / "sed").write_text(
        '#!/bin/sh\nif /usr/bin/sed --version >/dev/null 2>&1; then exec /usr/bin/sed "$@"; fi\n'
        'if [ "$1" = "-i" ]; then shift; exec /usr/bin/sed -i "" "$@"; fi\nexec /usr/bin/sed "$@"\n',
        encoding="utf-8")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "JOURNAL": str(journal),
           "UNITS": str(tmp_path / "units"), "ACTIVE": str(tmp_path / "active"),
           "RESOLVER_CONF": str(tmp_path / "resolver.conf"),
           "ROUTING_BASE_CONF": str(tmp_path / "base.conf"),
           "DROPIN_DIR": str(tmp_path / "dropin"),
           "EUID": "0"}
    (tmp_path / "active").write_text("")            # демон «жив»

    def run(*args):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                              text=True, env=env)

    def conf():
        p = tmp_path / "resolver.conf"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def log():
        return journal.read_text(encoding="utf-8") if journal.exists() else ""

    return type("H", (), {"run": staticmethod(run), "conf": staticmethod(conf),
                          "log": staticmethod(log), "dir": tmp_path})


def _listen(text):
    return [ln.split("=", 1)[1] for ln in text.splitlines() if ln.startswith("listen-address=")]


def test_install_writes_the_config_installs_the_package_and_restarts(host):
    r = host.run("install", "10.8.1.1")
    assert r.returncode == 0, r.stdout + r.stderr
    text = host.conf()
    assert _listen(text) == ["10.8.1.1"]
    assert "bind-dynamic" in text and "bind-interfaces" not in text, \
        "адрес интерфейса появляется вместе с ним — bind-interfaces не поднялся бы"
    assert "no-resolv" in text and "server=1.1.1.1" in text and "server=1.0.0.1" in text
    assert "stop-dns-rebind" in text
    assert "address=/cloudflare-dns.com/" in text and "address=/use-application-dns.net/" in text
    log = host.log()
    assert "apt-get install -y -q dnsmasq" in log, "юнита не было — пакет ставится"
    assert log.index("apt-get") > 0
    assert "systemctl restart dnsmasq" in log and "systemctl daemon-reload" in log
    dropin = (host.dir / "dropin" / "awgbot-resolver.conf").read_text(encoding="utf-8")
    assert "Restart=on-failure" in dropin, "упавший резолвер = люди без DNS; поднимаем сами"


def test_install_is_idempotent_and_skips_apt_when_the_unit_exists(host):
    (host.dir / "units").write_text("dnsmasq.service enabled\n", encoding="utf-8")
    assert host.run("install", "10.8.1.1").returncode == 0
    assert host.run("install", "10.8.1.1").returncode == 0
    assert _listen(host.conf()) == ["10.8.1.1"], "повтор не дублирует адрес"
    assert "apt-get" not in host.log()


def test_add_and_remove_keep_the_list_and_refuse_to_empty_it(host):
    assert host.run("install", "10.8.1.1").returncode == 0
    assert host.run("add", "10.9.1.1").returncode == 0
    assert _listen(host.conf()) == ["10.8.1.1", "10.9.1.1"]
    r = host.run("add", "10.9.1.1")
    assert r.returncode == 0 and "уже слушаем" in r.stdout
    assert host.run("remove", "10.8.1.1").returncode == 0
    assert _listen(host.conf()) == ["10.9.1.1"]
    r = host.run("remove", "10.9.1.1")
    assert r.returncode == 1 and "последний адрес" in r.stderr, \
        "снять последний адрес значит оставить клиентов без DNS"
    assert _listen(host.conf()) == ["10.9.1.1"]
    r = host.run("remove", "10.7.7.7")
    assert r.returncode == 0 and "и так не слушали" in r.stdout


def test_add_without_a_config_installs(host):
    assert host.run("add", "10.8.1.1").returncode == 0
    assert _listen(host.conf()) == ["10.8.1.1"] and "apt-get" in host.log()


def test_status_reports_addresses_and_liveness(host):
    r = host.run("status")
    assert r.returncode == 1 and "ADDRS=\n" in r.stdout
    host.run("install", "10.8.1.1")
    r = host.run("status")
    assert r.returncode == 0 and "ADDRS=10.8.1.1\n" in r.stdout and "ACTIVE=1" in r.stdout
    (host.dir / "active").unlink()
    r = host.run("status")
    assert r.returncode == 1 and "ACTIVE=0" in r.stdout


def test_install_fails_loudly_when_the_daemon_does_not_come_up(host):
    (host.dir / "active").unlink()
    r = host.run("install", "10.8.1.1")
    assert r.returncode == 1 and "не активен" in r.stderr


def test_adopting_the_routing_config_drops_bind_interfaces(host):
    """Прежний конфиг обвязки держал bind-interfaces — с bind-dynamic резолвера
    они несовместимы, dnsmasq не стартовал бы. Строку снимаем, адрес
    перехвата обвязки оставляем."""
    base = host.dir / "base.conf"
    base.write_text("bind-interfaces\nlisten-address=10.255.53.1\nlisten-address=10.9.1.1\nno-resolv\n",
                    encoding="utf-8")
    assert host.run("install", "10.9.1.1").returncode == 0
    text = base.read_text(encoding="utf-8")
    assert "bind-interfaces" not in text and "listen-address=10.255.53.1" in text
    assert "listen-address=10.9.1.1" not in text, "один адрес — в одном файле"
    assert host.run("add", "10.255.53.1").returncode == 0
    assert "listen-address=10.255.53.1" not in base.read_text(encoding="utf-8")


def test_plan_changes_nothing_and_needs_no_root(host):
    r = subprocess.run(["bash", str(SCRIPT), "plan", "10.8.1.1"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "RESOLVER_CONF": str(host.dir / "r.conf")})
    assert r.returncode == 0 and "would:" in r.stdout
    assert not (host.dir / "r.conf").exists()


@pytest.mark.parametrize("argv", [["install"], ["install", "nope"], ["add", "10.8.1"], ["bogus"]])
def test_bad_calls_are_refused(host, argv):
    assert host.run(*argv).returncode == 2


def test_script_is_executable_in_the_index():
    out = subprocess.run(["git", "ls-files", "-s", "install/awg-resolver-setup.sh"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    assert out.startswith("100755"), out
