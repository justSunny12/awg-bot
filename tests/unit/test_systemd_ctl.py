"""infra/systemd: start / stop / reset_failed и живость гостя резерва через
is_active.

Что стережём: каждый вызов systemctl — с потолком; start с ненулевым кодом —
SubprocessError «код N» (как у копий в резерве, которые ловят именно его), stop
и reset-failed — код возврата без исключения (юнита нет или он не в failed —
не отказ); нет systemctl или завис — исключение ОС/таймаута как есть, решает
вызывающий. Живость гостя (rsvzone.guest_unit_active) — `is-active` с потолком
15 с, молчащий systemctl — «не жив», а не исключение. Снятие таймера отката
файервола — stop и reset-failed таймера и сервиса, сбой — строка журнала.

Цена ошибки: start, принявший ненулевой код за успех, — резерв считает гостя
поднятым, а бот основы на резерве не отвечает; вызов без потолка — повисший на
D-Bus systemctl держит такт резерва без предела; исключение из
guest_unit_active — такт приёма падает вместо повторного пуска гостя.
"""
from __future__ import annotations

import subprocess

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


def test_start_stop_and_reset_failed_call_systemctl_with_a_ceiling(monkeypatch):
    seen = _rec(monkeypatch)
    systemd.start("awg-guest.service")
    assert systemd.stop("awg-guest.service") == 0
    assert systemd.reset_failed("awg-guest.service", timeout=5) == 0
    assert seen == [(["systemctl", "start", "awg-guest.service"], 60),
                    (["systemctl", "stop", "awg-guest.service"], 60),
                    (["systemctl", "reset-failed", "awg-guest.service"], 5)]


def test_a_failed_start_is_an_error_and_a_failed_stop_is_a_code(monkeypatch):
    _rec(monkeypatch, rc=5)
    with pytest.raises(subprocess.SubprocessError, match="^код 5$"):
        systemd.start("x.service")
    assert systemd.stop("x.service") == 5 and systemd.reset_failed("x.service") == 5


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "systemctl"), subprocess.TimeoutExpired(["systemctl"], 60)])
def test_no_systemctl_or_a_hang_reaches_the_caller(monkeypatch, exc):
    _rec(monkeypatch, exc=exc)
    for call in (systemd.start, systemd.stop, systemd.reset_failed):
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
