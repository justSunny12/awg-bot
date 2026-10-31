"""
Файервол ВПС — вызовы systemctl и systemd-run с потолком и без падения.

Стережём: без timeout зависший systemd держал CLI файервола и поток бота
бесконечно (правило «наружу и в систему ≤ 10 с»); без systemctl (контейнер,
стенд) FileNotFoundError из ensure_persistence ронял применение уже
поставленных правил, а снятие таймера отката — весь `confirm`.
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.infra import nftguard

pytestmark = pytest.mark.unit


def _rec(monkeypatch, exc=None):
    seen = []

    def fake_run(argv, **kw):
        seen.append((list(argv), kw.get("timeout")))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


@pytest.fixture()
def main_conf(monkeypatch, tmp_path):
    monkeypatch.setattr(nftguard, "MAIN_CONF", str(tmp_path / "nftables.conf"))


def test_every_systemctl_call_has_a_ceiling(monkeypatch, main_conf):
    seen = _rec(monkeypatch)
    nftguard.ensure_persistence()
    nftguard.disarm_rollback()
    nftguard.arm_rollback(60, ["true"], {})
    calls = [(a, t) for a, t in seen if a[0] in ("systemctl", "systemd-run")]
    assert calls and all(t is not None and t <= 10 for _a, t in calls), calls


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "systemctl"),
                                 subprocess.TimeoutExpired(["systemctl"], 10)])
def test_no_systemctl_or_a_hang_is_a_warning_not_a_crash(monkeypatch, main_conf, exc):
    _rec(monkeypatch, exc)
    done = nftguard.ensure_persistence()
    assert any("ВНИМАНИЕ" in d for d in done)
    nftguard.disarm_rollback()                                 # не бросает
    with pytest.raises(nftguard.GuardError, match="таймер отката не поставлен"):
        nftguard.arm_rollback(60, ["true"], {})
