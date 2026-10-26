"""
install/awg-admin-dns-setup.sh — резолвер админа («SMB в туннеле»): второй
dnsmasq на своём /32. Прогоном с подставными systemctl/dnsmasq/ip в PATH, а
не чтением исходника.

Цена ошибки: резолвер админа — единственный DNS новых конфигов устройств
админа. Конфиг без работающего демона, оставленный «поставленным», — у
админа нет имён вообще (не только SMB); файл записей, принятый вслепую,
роняет экземпляр в цикл рестартов; лишний рестарт на каждом старте бота —
сброшенный кэш и минута без DNS у админа.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.botscript import flat
from tests.conftest import fake_cmd

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install" / "awg-admin-dns-setup.sh"
UPSTREAM = "10.9.1.1"           # резолвер пользователей — <подсеть>.1
ADDR = "10.255.53.53"


@pytest.fixture()
def host(tmp_path):
    """Подставной хост: systemctl/ip пишут в журнал; is-active — файл ACTIVE;
    restart падает при RESTART_RC≠0; dnsmasq --test — код DNSMASQ_RC."""
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    journal = tmp_path / "journal"
    fake_cmd(bin_dir / "systemctl",
             '#!/bin/sh\necho "systemctl $*" >> "$JOURNAL"\n'
             'case "$1" in is-active) [ -f "$ACTIVE" ] ;; restart) exit "${RESTART_RC:-0}" ;; esac\n')
    fake_cmd(bin_dir / "dnsmasq", '#!/bin/sh\necho "dnsmasq $*" >> "$JOURNAL"\n'
                                  '[ "${DNSMASQ_RC:-0}" = 0 ] || echo "bad option at line 3" >&2\n'
                                  'exit "${DNSMASQ_RC:-0}"\n')
    fake_cmd(bin_dir / "ip", '#!/bin/sh\necho "ip $*" >> "$JOURNAL"\n')
    admin_dir = tmp_path / "etc-admin-dns"
    unit = tmp_path / "systemd" / "awgbot-admin-dns.service"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "JOURNAL": str(journal),
           "ACTIVE": str(tmp_path / "active"), "ADMIN_DIR": str(admin_dir),
           "UNIT_FILE": str(unit), "LOCK": str(tmp_path / "admin-dns.lock"), "EUID": "0"}
    (tmp_path / "active").write_text("")            # демон «жив»

    def run(*args, **over):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                              env={**env, **over})

    def read(p: Path) -> str:
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def log() -> str:
        return read(journal)

    def reset_log() -> None:
        journal.write_text("", encoding="utf-8")

    return type("H", (), {
        "run": staticmethod(run), "log": staticmethod(log), "reset_log": staticmethod(reset_log),
        "conf": staticmethod(lambda: read(admin_dir / "dnsmasq.conf")),
        "unit": staticmethod(lambda: read(unit)),
        "records": staticmethod(lambda: read(admin_dir / "records.d" / "awg-smb.conf")),
        "dir": tmp_path, "admin_dir": admin_dir, "unit_path": unit,
        "records_path": admin_dir / "records.d" / "awg-smb.conf",
        "alive": staticmethod(lambda on: (tmp_path / "active").write_text("") if on
                              else (tmp_path / "active").unlink(missing_ok=True)),
    })


def _records_file(host, text: str) -> Path:
    p = host.dir / "rec.conf"
    p.write_text(text, encoding="utf-8")
    return p


REC = ("# awg-bot (сервер AWG): SMB локальных сетей шлюзов для устройств админа.\n"
       "local=/awg.internal/\nhost-record=naspi5.awg.internal,192.168.1.10\n")


# ── install ──────────────────────────────────────────────────────────────────

def test_install_writes_its_own_config_and_unit_and_starts_the_instance(host):
    """Свой конфиг целиком, не /etc/dnsmasq.d: однократные ключи основного
    экземпляра не мешают; отвечает только на своём адресе, всё, кроме
    awg.internal, — резолверу пользователей (кэш и блок DoH наследуются)."""
    r = host.run("install", UPSTREAM)
    assert r.returncode == 0, r.stdout + r.stderr
    conf = host.conf().splitlines()
    for line in (f"listen-address={ADDR}", "bind-interfaces", "no-resolv", "no-hosts",
                 f"server={UPSTREAM}", f"conf-dir={host.admin_dir}/records.d,*.conf"):
        assert line in conf, f"нет «{line}» в конфиге резолвера админа:\n{host.conf()}"
    assert (host.admin_dir / "records.d").is_dir(), "каталог записей не создан — conf-dir укажет в пустоту"
    unit = host.unit()
    assert f"--conf-file={host.admin_dir}/dnsmasq.conf" in unit and " -k " in unit, unit
    assert f"addr replace {ADDR}/32 dev awgadmdns" in unit, "адрес /32 на dummy — иначе слушать нечего"
    assert "ExecStopPost" not in unit, \
        "dummy снимается при остановке — запрос админа ушёл бы маршрутом по умолчанию к провайдеру"
    assert "Restart=on-failure" in unit and "StartLimitIntervalSec=0" in unit, \
        "упавший резолвер админа — устройства админа без DNS; поднимаем сами, без лимита"
    log = host.log()
    assert "systemctl daemon-reload" in log and "systemctl restart awgbot-admin-dns" in log, log
    assert f"dnsmasq --test --conf-file={host.admin_dir}/dnsmasq.conf" in log, "до рестарта нет проверки"


def test_install_again_with_the_same_upstream_does_not_restart(host):
    """Бот зовёт install на каждом старте: рестарт без изменений — сброшенный
    кэш и окно без DNS у админа на каждом обновлении бота."""
    assert host.run("install", UPSTREAM).returncode == 0
    host.reset_log()
    r = host.run("install", UPSTREAM)
    assert r.returncode == 0 and "без изменений" in r.stdout, r.stdout + r.stderr
    assert "restart" not in host.log() and "daemon-reload" not in host.log(), host.log()


def test_install_restarts_a_dead_instance_even_without_changes(host):
    assert host.run("install", UPSTREAM).returncode == 0
    host.alive(False)
    host.reset_log()
    host.run("install", UPSTREAM)
    assert "systemctl restart awgbot-admin-dns" in host.log(), "лежащий экземпляр не поднят"


def test_a_moved_client_resolver_moves_the_upstream(host):
    """Переезд подсети пользователей меняет адрес резолвера пользователей — server=
    тянется за ним, иначе всё, кроме SMB, у админа уходит в никуда."""
    assert host.run("install", UPSTREAM).returncode == 0
    host.reset_log()
    assert host.run("install", "10.10.1.1").returncode == 0
    assert "server=10.10.1.1" in host.conf().splitlines() and f"server={UPSTREAM}" not in host.conf()
    assert "systemctl restart awgbot-admin-dns" in host.log()


def test_a_refused_config_on_update_puts_the_previous_one_back_and_keeps_the_daemon(host):
    """dnsmasq --test отверг новый конфиг — прежний на месте, работающий демон
    не перезапускается: рестарт на прежнем конфиге — лишнее окно без DNS."""
    assert host.run("install", UPSTREAM).returncode == 0
    before = host.conf()
    host.reset_log()
    r = host.run("install", "10.10.1.1", DNSMASQ_RC="1")
    assert r.returncode == 1 and "отклонён" in r.stderr, r.stdout + r.stderr
    assert host.conf() == before, "прежний рабочий конфиг не возвращён"
    assert "systemctl restart" not in host.log(), host.log()


def test_a_restart_failure_on_update_puts_the_previous_config_back(host):
    assert host.run("install", UPSTREAM).returncode == 0
    before = host.conf()
    r = host.run("install", "10.10.1.1", RESTART_RC="1")
    assert r.returncode == 1 and "не поднялся" in r.stderr, r.stdout + r.stderr
    assert host.conf() == before


def test_a_refused_config_on_first_install_leaves_nothing(host):
    r = host.run("install", UPSTREAM, DNSMASQ_RC="1")
    assert r.returncode == 1
    assert not (host.admin_dir / "dnsmasq.conf").exists() and not host.unit_path.exists(), \
        "конфиг первой, отвергнутой установки остался — бот сочтёт резолвер админа поставленным"


def test_a_failed_first_install_does_not_leave_the_resolver_looking_installed(host):
    """Первая установка не поднялась — конфига быть не должно: бот считает
    резолвер админа поставленным по наличию конфига (admindns.installed) и
    тогда отдаёт его адрес в DNS новых конфигов устройств админа и вешает
    правило nft на его :53. Конфиг без живого демона — админ без DNS совсем.
    Так же устроен резолвер пользователей: «первая установка — файл снимается»."""
    host.alive(False)
    r = host.run("install", UPSTREAM, RESTART_RC="1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert not (host.admin_dir / "dnsmasq.conf").exists(), \
        "конфиг первой, не поднявшейся установки остался — бот сочтёт резолвер админа поставленным"


def test_install_without_dnsmasq_refuses(host, tmp_path):
    (tmp_path / "bin" / "dnsmasq").unlink()
    r = host.run("install", UPSTREAM)
    assert r.returncode == 1 and "нет dnsmasq" in r.stderr
    assert host.conf() == "" and host.unit() == ""


def test_install_needs_root(host):
    r = host.run("install", UPSTREAM, EUID="1000")
    assert r.returncode == 1 and "нужен root" in r.stderr
    assert host.conf() == ""


def test_plan_changes_nothing_and_needs_no_root(host, tmp_path):
    """plan — из `awg-bot resolver admin plan` без sudo и на хосте без dnsmasq."""
    r = subprocess.run(["bash", str(SCRIPT), "plan", UPSTREAM], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "ADMIN_DIR": str(host.admin_dir),
                            "UNIT_FILE": str(host.unit_path), "EUID": "1000"})
    assert r.returncode == 0 and "would:" in r.stdout, r.stdout + r.stderr
    assert not host.admin_dir.exists() and not host.unit_path.exists(), "plan что-то записал"


@pytest.mark.parametrize("argv,over", [
    (["install"], {}), (["install", "nope"], {}), (["plan", "10.9.1"], {}),
    (["install", UPSTREAM], {"ADMIN_ADDR": "10.255.53"}), (["records"], {}), (["bogus"], {}), ([], {})])
def test_bad_calls_are_refused_with_code_2(host, argv, over):
    r = host.run(*argv, **over)
    assert r.returncode == 2, (argv, r.returncode, r.stdout, r.stderr)
    assert host.conf() == ""


# ── records ──────────────────────────────────────────────────────────────────

def test_records_are_put_in_place_checked_and_the_instance_restarted(host):
    assert host.run("install", UPSTREAM).returncode == 0
    host.reset_log()
    r = host.run("records", str(_records_file(host, REC)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert host.records() == REC
    log = host.log()
    assert log.index("dnsmasq --test") < log.index("systemctl restart awgbot-admin-dns"), \
        "рестарт раньше проверки — битый файл уронил бы экземпляр"


def test_the_same_records_again_do_not_restart(host):
    assert host.run("install", UPSTREAM).returncode == 0
    assert host.run("records", str(_records_file(host, REC))).returncode == 0
    host.reset_log()
    r = host.run("records", str(_records_file(host, REC)))
    assert r.returncode == 0 and "не изменились" in r.stdout
    assert "restart" not in host.log()


def test_refused_records_put_the_previous_ones_back(host):
    """dnsmasq отверг новый файл — на месте прежний, экземпляр поднят на нём."""
    assert host.run("install", UPSTREAM).returncode == 0
    assert host.run("records", str(_records_file(host, REC))).returncode == 0
    new = REC + "host-record=backup.awg.internal,192.168.1.20\n"
    r = host.run("records", str(_records_file(host, new)), DNSMASQ_RC="1")
    assert r.returncode == 1 and "прежние на месте" in r.stderr, r.stdout + r.stderr
    assert host.records() == REC


def test_refused_first_records_leave_no_file(host):
    assert host.run("install", UPSTREAM).returncode == 0
    r = host.run("records", str(_records_file(host, REC)), RESTART_RC="1")
    assert r.returncode == 1
    assert not host.records_path.exists(), "непринятый файл записей остался в conf-dir"


def test_dash_removes_the_records_and_twice_is_a_noop(host):
    assert host.run("install", UPSTREAM).returncode == 0
    assert host.run("records", str(_records_file(host, REC))).returncode == 0
    host.reset_log()
    assert host.run("records", "-").returncode == 0
    assert not host.records_path.exists()
    assert "systemctl restart awgbot-admin-dns" in host.log(), "записи сняты, а экземпляр их ещё отдаёт"
    host.reset_log()
    r = host.run("records", "-")
    assert r.returncode == 0 and "и так нет" in r.stdout and "restart" not in host.log()


def test_records_without_the_resolver_refuse(host):
    r = host.run("records", str(_records_file(host, REC)))
    assert r.returncode == 1 and "сначала install" in r.stderr
    assert not host.records_path.exists()


def test_records_from_a_missing_file_are_a_bad_call(host):
    assert host.run("install", UPSTREAM).returncode == 0
    assert host.run("records", str(host.dir / "nope.conf")).returncode == 2


# ── status и remove ──────────────────────────────────────────────────────────

def test_status_reports_address_liveness_upstream_and_records(host):
    r = host.run("status")
    assert r.returncode == 1 and f"ADDR={ADDR}\n" in r.stdout and "ACTIVE=1" in r.stdout \
        and "UPSTREAM=\n" in r.stdout, "без конфига — не поставлен, даже если юнит «жив»"
    host.run("install", UPSTREAM)
    r = host.run("status")
    assert r.returncode == 0 and f"UPSTREAM={UPSTREAM}\n" in r.stdout and "RECORDS=0" in r.stdout, r.stdout
    host.run("records", str(_records_file(host, REC)))
    assert "RECORDS=1" in host.run("status").stdout
    host.alive(False)
    r = host.run("status")
    assert r.returncode == 1 and "ACTIVE=0" in r.stdout


def test_remove_takes_unit_config_records_and_interface(host):
    host.run("install", UPSTREAM)
    host.run("records", str(_records_file(host, REC)))
    host.reset_log()
    r = host.run("remove")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not host.admin_dir.exists() and not host.unit_path.exists()
    log = host.log()
    assert "systemctl disable --now awgbot-admin-dns" in log and "ip link del awgadmdns" in log, log
    assert host.run("remove").returncode == 0, "повторное снятие — не ошибка"


def test_remove_needs_root(host):
    host.run("install", UPSTREAM)
    r = host.run("remove", EUID="1000")
    assert r.returncode == 1 and host.conf() != ""


# ── awg-bot resolver admin … ─────────────────────────────────────────────────

def _cli_admin(tmp_path, *args, euid="0", admin_dns=None):
    """cmd_resolver из awg-bot.sh (с настоящими yaml_get и admin_dns_addr):
    скрипты резолверов подменены журналом «кто, ADMIN_ADDR, аргументы»."""
    import tempfile
    from tests.unit.test_cli_script import _extract_func
    src = flat()
    tmp = Path(tempfile.mkdtemp(dir=tmp_path))
    inst = tmp / "inst"; (inst / "install").mkdir(parents=True)
    conf = tmp / "conf"; conf.mkdir()
    (conf / "app.yaml").write_text("client_config:\n  dns1: \"10.9.1.1\"\n"
                                   + (f'  admin_dns: "{admin_dns}"\n' if admin_dns is not None else ""),
                                   encoding="utf-8")
    calls = tmp / "calls"
    for name, who in (("awg-admin-dns-setup.sh", "admin"), ("awg-resolver-setup.sh", "client")):
        (inst / "install" / name).write_text(f'echo "{who} [$ADMIN_ADDR] $*" >> "{calls}"\n', encoding="utf-8")
    prog = "\n".join([
        f'INSTALL_DIR="{inst}"', f'CONF_DIR="{conf}"',
        'die() { echo "die: $*" >&2; exit 1; }',
        "require_installed() { :; }",
        'resolver_addr() { echo 10.9.1.1; }',
        re.search(r"^require_root\(\).*$", src, re.M).group(0),
        _extract_func(src, "yaml_get"),
        _extract_func(src, "admin_dns_addr"),
        _extract_func(src, "cmd_resolver"),
        "cmd_resolver " + " ".join(args),
    ])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "EUID": euid})
    return r, (calls.read_text(encoding="utf-8") if calls.exists() else "")


@pytest.mark.parametrize("args,want", [
    (("admin", "plan"), "admin [10.255.53.53] plan 10.9.1.1\n"),
    (("admin", "install"), "admin [10.255.53.53] install 10.9.1.1\n"),
    (("admin", "status"), "admin [10.255.53.53] status\n"), (("admin",), "admin [10.255.53.53] status\n"),
    (("admin", "remove"), "admin [10.255.53.53] remove\n")])
def test_cli_admin_goes_to_the_admin_script_with_the_client_resolver_as_upstream(tmp_path, args, want):
    """`awg-bot resolver admin …` — к своему скрипту, а не к резолверу пользователей:
    перепутай — remove снял бы DNS всех пользователей."""
    r, calls = _cli_admin(tmp_path, *args)
    assert r.returncode == 0, r.stderr
    assert calls == want, calls


@pytest.mark.parametrize("value,want", [("10.255.9.9", "10.255.9.9"), ("10.255.9", "10.255.53.53"),
                                        ("", "10.255.53.53")])
def test_cli_admin_takes_the_address_from_the_same_setting_as_the_bot(tmp_path, value, want):
    """Адрес из client_config.admin_dns: иначе скрипт поднял бы резолвер на
    10.255.53.53, а бот раздал бы админу и вписал в nft другой адрес."""
    r, calls = _cli_admin(tmp_path, "admin", "install", admin_dns=value)
    assert r.returncode == 0 and calls == f"admin [{want}] install 10.9.1.1\n", (r.stderr, calls)


def test_cli_admin_install_and_remove_need_root_but_plan_and_status_do_not(tmp_path):
    for sub in ("install", "remove"):
        r, calls = _cli_admin(tmp_path, "admin", sub, euid="1000")
        assert r.returncode == 1 and "нужен root" in r.stderr and calls == "", (sub, r.stderr, calls)
    for sub in ("plan", "status"):
        r, calls = _cli_admin(tmp_path, "admin", sub, euid="1000")
        assert r.returncode == 0 and calls.startswith(f"admin [10.255.53.53] {sub}"), (sub, r.stderr, calls)


def test_uninstall_takes_the_admin_resolver_with_it():
    """Удаление бота снимает и резолвер админа — без скрипта поставки (каталог
    приложения к этому моменту может быть уже снят): иначе на хосте остался
    бы dnsmasq на 10.255.53.53 и dummy-интерфейс, о которых никто не помнит."""
    from tests.unit.test_cli_script import _extract_func
    body = _extract_func(flat(), "cmd_post_uninstall")
    assert "-f /etc/systemd/system/awgbot-admin-dns.service" in body, "ветка снятия не видит резолвер админа"
    for cmd in ("systemctl disable --now awgbot-admin-dns", "rm -f /etc/systemd/system/awgbot-admin-dns.service",
                "ip link del awgadmdns", "rm -rf /etc/awgbot-admin-dns"):
        assert cmd in body, f"uninstall не делает «{cmd}»"


def test_first_client_resolver_install_also_installs_the_admin_resolver():
    """Установщик ставит резолвер админа вместе с резолвером пользователей, на его
    адрес; отказ — предупреждение, не обрыв установки (поставит бот)."""
    from tests.unit.test_cli_script import _extract_func
    body = _extract_func(flat(), "ensure_resolver")
    lines = body.splitlines()
    i = next((i for i, ln in enumerate(lines) if "awg-admin-dns-setup.sh" in ln), None)
    assert i is not None and 'install "$addr"' in lines[i], body
    assert 'ADMIN_ADDR="$(admin_dns_addr)"' in lines[i], "установщик ставит резолвер админа не на адрес из настройки"
    assert "|| warn" in lines[i + 1], "отказ резолвера админа обрывает установку"
