"""infra/systemd: start / stop / reset_failed, restart / enable / disable /
daemon_reload / unit_file_listed и живость гостя резерва через is_active.

restart, enable, disable и daemon-reload отдают итог целиком (код и вывод):
вызывающие — перезапуск dnsmasq списков, включение nftables, юнит обвязки
шлюза (перечитывание и реассерт), снятие интерфейса после переезда, перезапуск
агента командой подключения — показывают человеку хвост stderr и держат свои
прежние потолки. Наличие юнита dnsmasq при развёртывании РФ-доступа — тоже с
потолком (раньше его не было). Сторож: прямых `systemctl restart|enable|
disable|daemon-reload` вне infra/systemd больше нет (кроме перечисленных ниже).

Что стережём: каждый вызов systemctl — с потолком; start с ненулевым кодом —
SubprocessError «код N» (как у копий в резерве, которые ловят именно его), stop
и reset-failed — код возврата без исключения (юнита нет или он не в failed —
не отказ); нет systemctl или завис — исключение ОС/таймаута как есть, решает
вызывающий. Живость гостя (rsvzone.guest_unit_active) — `is-active` с потолком
15 с, молчащий systemctl — «не жив», а не исключение. Снятие таймера отката
файервола — stop и reset-failed таймера и сервиса, сбой — строка журнала.

Цена ошибки: start, принявший ненулевой код за успех, — резерв считает гостя
поднятым, а гость не отвечает; вызов без потолка — повисший на
D-Bus systemctl держит такт резерва без предела; исключение из
guest_unit_active — такт приёма падает вместо повторного пуска гостя;
потерянный stderr перезапуска — «не перезапустить dnsmasq» без причины;
юнит обвязки, о котором systemd не узнал, — новые значения на диске без
таблицы под ними.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from awgbot.infra import systemd

pytestmark = pytest.mark.unit


def _rec(monkeypatch, rc=0, exc=None):
    seen = []

    def fake(argv, **kw):
        seen.append((list(argv), kw.get("timeout")))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, rc, b"", b"")
    monkeypatch.setattr(subprocess, "run", fake)
    return seen


def test_stop_and_reset_failed_call_systemctl_with_a_ceiling(monkeypatch):
    seen = _rec(monkeypatch)
    assert systemd.stop("awg-guest.service") == 0
    assert systemd.reset_failed("awg-guest.service", timeout=5) == 0
    assert seen == [(["systemctl", "stop", "awg-guest.service"], 60),
                    (["systemctl", "reset-failed", "awg-guest.service"], 5)]


def test_a_failed_stop_is_a_code(monkeypatch):
    _rec(monkeypatch, rc=5)
    assert systemd.stop("x.service") == 5 and systemd.reset_failed("x.service") == 5


def test_there_is_no_start_helper():
    """Пуск юнитов машина резерва делает сама (тесты наблюдают его подменой в её модулях):
    помощник пуска без вызывающих — мёртвый код."""
    assert not hasattr(systemd, "start")


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "systemctl"), subprocess.TimeoutExpired(["systemctl"], 60)])
def test_no_systemctl_or_a_hang_reaches_the_caller(monkeypatch, exc):
    _rec(monkeypatch, exc=exc)
    for call in (systemd.stop, systemd.reset_failed):
        with pytest.raises((OSError, subprocess.SubprocessError)):
            call("x.service")


@pytest.mark.parametrize("rc, exc, want", [(0, None, True), (3, None, False),
                                           (0, subprocess.TimeoutExpired(["systemctl"], 15), False),
                                           (0, FileNotFoundError(2, "systemctl"), False)],
                         ids=["active", "inactive", "hang", "no-systemctl"])
def test_the_guest_unit_liveness(monkeypatch, rc, exc, want):
    from awgbot.infra import rsvzone
    seen = _rec(monkeypatch, rc=rc, exc=exc)
    assert rsvzone.guest_unit_active() is want
    assert seen == [(["systemctl", "is-active", "--quiet", rsvzone.GUEST_UNIT], 15)]


def test_disarming_the_firewall_rollback_stops_and_resets_both_units(monkeypatch):
    from awgbot.infra import nftguard
    seen = _rec(monkeypatch, rc=1)
    nftguard.disarm_rollback()
    ctl = [a for a, _t in seen if a[1] in ("stop", "reset-failed")]
    unit = nftguard.ROLLBACK_UNIT
    assert ctl == [["systemctl", "stop", f"{unit}.timer"], ["systemctl", "reset-failed", f"{unit}.timer"],
                   ["systemctl", "stop", f"{unit}.service"], ["systemctl", "reset-failed", f"{unit}.service"]]
    assert all(t is not None and t <= 10 for _a, t in seen)


# ── restart / enable / disable / daemon-reload / list-unit-files ────────────

def _host(monkeypatch, answers=None):
    """subprocess.run: запись (argv, timeout); answers — argv[:2] → (rc, stdout, stderr) или исключение."""
    seen = []
    answers = answers or {}

    def fake(argv, **kw):
        seen.append((list(argv), kw.get("timeout")))
        got = answers.get(tuple(argv[:2]), (0, b"", b""))
        if isinstance(got, BaseException):
            raise got
        rc, out, err = got
        return subprocess.CompletedProcess(argv, rc, out, err)
    monkeypatch.setattr(subprocess, "run", fake)
    return seen


def test_restart_enable_disable_and_reload_give_the_whole_outcome_with_a_ceiling(monkeypatch):
    seen = _host(monkeypatch, {("systemctl", "restart"): (1, b"", b"job failed")})
    proc = systemd.restart("dnsmasq", timeout=30)
    assert proc.returncode == 1 and proc.stderr == b"job failed", "отказ перезапуска — итог, не исключение"
    assert systemd.enable("nftables").returncode == 0 and systemd.disable("awg-quick@awg1").returncode == 0
    assert systemd.daemon_reload().returncode == 0
    assert seen == [(["systemctl", "restart", "dnsmasq"], 30), (["systemctl", "enable", "nftables"], 30),
                    (["systemctl", "disable", "awg-quick@awg1"], 30), (["systemctl", "daemon-reload"], 30)]
    for exc in (FileNotFoundError(2, "systemctl"), subprocess.TimeoutExpired(["systemctl"], 30)):
        _host(monkeypatch, {("systemctl", v): exc for v in ("restart", "enable", "disable", "daemon-reload")})
        for call in (lambda: systemd.restart("x"), lambda: systemd.enable("x"), lambda: systemd.disable("x"),
                     systemd.daemon_reload):
            with pytest.raises((OSError, subprocess.SubprocessError)):
                call()


@pytest.mark.parametrize("rc, out, want", [(0, b"dnsmasq.service enabled enabled\n", True), (0, b"", False),
                                           (1, b"dnsmasq.service enabled", False)],
                         ids=["listed", "only-the-binary", "refused"])
def test_a_unit_file_is_listed_only_with_its_name_in_the_output(monkeypatch, rc, out, want):
    seen = _host(monkeypatch, {("systemctl", "list-unit-files"): (rc, out, b"")})
    assert systemd.unit_file_listed("dnsmasq.service") is want
    assert seen == [(["systemctl", "list-unit-files", "dnsmasq.service"], 30)]


def test_the_lists_dnsmasq_restart_keeps_its_ceiling_and_its_reason(monkeypatch, tmp_path):
    from awgbot.core import config
    from awgbot.infra.routing import RoutingError, feeds
    seen = _host(monkeypatch, {("systemctl", "restart"): (1, b"", b"Job for dnsmasq.service failed")})
    with pytest.raises(RoutingError, match="^Не перезапустить dnsmasq: Job for dnsmasq.service failed$"):
        feeds.write_dnsmasq_conf("ipset=/x/awg\n", str(tmp_path / "lists.conf"))
    assert (["systemctl", "restart", config.ROUTING_DNSMASQ_SERVICE], 30) in seen


def test_enabling_nftables_keeps_the_short_ceiling(monkeypatch, tmp_path):
    from awgbot.infra import nftguard
    main = tmp_path / "nftables.conf"
    main.write_text('include "/etc/nftables.d/*.nft"\n', encoding="utf-8")
    monkeypatch.setattr(nftguard, "MAIN_CONF", str(main))
    monkeypatch.setattr(nftguard, "RULES_DIR", "/etc/nftables.d")
    seen = _host(monkeypatch)
    assert nftguard.ensure_persistence() == ["nftables.service включён"]
    assert seen == [(["systemctl", "enable", "nftables"], 10)]
    _host(monkeypatch, {("systemctl", "enable"): subprocess.TimeoutExpired(["systemctl"], 10)})
    assert "ВНИМАНИЕ" in nftguard.ensure_persistence()[-1], "завис — предупреждение, а не падение"


@pytest.mark.parametrize("answer, want", [((0, b"", b""), (True, "")),
                                          ((1, b"", b"x" * 400 + b"fail"), (False, "x" * 296 + "fail")),
                                          (OSError(2, "systemctl"), (False, "systemctl не запустился")),
                                          (subprocess.TimeoutExpired(["systemctl"], 90), (False, "timeout"))],
                         ids=["ok", "refused", "no-systemctl", "hang"])
def test_the_gateway_reassert_restarts_the_plumbing_unit_with_its_ceiling(monkeypatch, answer, want):
    from awgbot.core import config
    from awgbot.infra import gwguard
    seen = _host(monkeypatch, {("systemctl", "restart"): answer})
    ok, err = gwguard.reassert(timeout=90)
    assert seen == [(["systemctl", "restart", config.GW_UNIT], 90)]
    if want[1] == "timeout":
        assert ok is False and gwguard.is_timeout(err), err
    elif want[1].startswith("systemctl"):
        assert ok is False and err.startswith(want[1]), err
    else:
        assert (ok, err) == want


def test_the_gateway_unit_reload_refusal_carries_the_reason(monkeypatch):
    from awgbot.infra import gwguard
    seen = _host(monkeypatch, {("systemctl", "daemon-reload"): (1, b"", b"Access denied")})
    with pytest.raises(gwguard.GwGuardError, match="отказал: Access denied$"):
        gwguard._daemon_reload()
    _host(monkeypatch, {("systemctl", "daemon-reload"): subprocess.TimeoutExpired(["systemctl"], 30)})
    with pytest.raises(gwguard.GwGuardError, match="не отработал"):
        gwguard._daemon_reload()
    assert seen == [(["systemctl", "daemon-reload"], 30)]


def test_retiring_an_interface_downs_it_and_takes_it_off_autostart(monkeypatch, caplog):
    from awgbot.domain.migration.exits import MigrationExitsMixin
    seen = _host(monkeypatch, {("systemctl", "disable"): FileNotFoundError(2, "systemctl")})
    with caplog.at_level("WARNING"):
        MigrationExitsMixin._retire_interface("awg1")
    assert seen == [(["awg-quick", "down", "awg1"], 30), (["systemctl", "disable", "awg-quick@awg1"], 30)]
    assert "promote: systemctl disable awg-quick@awg1 не выполнено" in caplog.text


def test_the_join_command_restarts_the_agent_with_its_ceiling(monkeypatch, tmp_path):
    import types
    from awgbot.domain.gateway import join
    from awgbot.infra import gwguard
    from awgbot.util import jointoken
    from tools import join as cli
    monkeypatch.setattr(join, "JOIN_FILE", str(tmp_path / "join.json"))
    monkeypatch.setattr(join, "STATUS_FILE", str(tmp_path / "join.status"))
    monkeypatch.setattr(jointoken, "decode", lambda text: types.SimpleNamespace(
        join_id="j1", slot=2, version="4.2.0", expires=4102444800, host="203.0.113.1", channel_port=8787,
        link_port=51820))
    monkeypatch.setattr(cli, "_link_conf", lambda t: "[Interface]\n")
    monkeypatch.setattr(gwguard, "join_link", lambda conf, port, script="": (True, ""))
    seen = _host(monkeypatch, {("systemctl", "restart"): (1, b"", b"")})
    assert cli.cmd_join("токен") == 1
    assert seen == [(["systemctl", "restart", "awg-bot"], 120)]


# ── сторож: systemctl — одним способом ──────────────────────────────────────

# машина резерва (её такты наблюдают тесты через подмену subprocess модулей),
# отдельный перезапуск обновления (spawn_detached) и обвязка SSH — переводятся отдельно
_CTL_ALLOWED = {"awgbot/infra/systemd.py", "awgbot/domain/selfupdate/core.py", "awgbot/infra/s" + "shd.py"}


def test_no_module_runs_systemctl_restart_enable_disable_or_reload_itself():
    root = Path(__file__).resolve().parents[2]
    pat = re.compile(r'"systemctl",\s*"(restart|enable|disable|daemon-reload)"')
    bad = []
    for p in sorted([*(root / "awgbot").rglob("*.py"), *(root / "tools").rglob("*.py")]):
        rel = p.relative_to(root).as_posix()
        if rel in _CTL_ALLOWED or re.search(r"/reserve/(inbound|outbound)[^/]*\.py$|/reserve/openproc\.py$", rel):
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                bad.append(f"{rel}:{n}: {line.strip()}")
    assert bad == [], "systemctl — через infra/systemd:\n" + "\n".join(bad)
