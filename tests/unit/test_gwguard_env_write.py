"""
Файервол шлюза — запись firewall.env.

Стережём: сбой записи (ENOSPC, обрыв) не оставляет временного файла рядом с
firewall.env (раньше .firewall.env.* копились в /etc/awg-gw при каждом
сбое) и не трогает прежний файл; права итога — 0644, как и были.
"""
from __future__ import annotations

import os
import stat

import pytest

from awgbot.infra import gwguard

pytestmark = pytest.mark.unit


@pytest.fixture()
def env(monkeypatch, tmp_path):
    p = tmp_path / "firewall.env"
    monkeypatch.setattr(gwguard, "FW_ENV", str(p))
    return p


def test_a_failed_write_leaves_no_tmp_and_the_old_file(env, monkeypatch):
    gwguard.write_env(SSH_PORT="2222")
    old = env.read_text()

    def boom(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        gwguard.write_env(SSH_PORT="2223")
    assert env.read_text() == old
    assert [p.name for p in env.parent.iterdir()] == ["firewall.env"]


def test_the_env_stays_world_readable(env):
    gwguard.write_env(SSH_PORT="2222")
    assert stat.S_IMODE(env.stat().st_mode) == 0o644
    assert 'SSH_PORT="2222"' in env.read_text()
