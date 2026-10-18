"""awg-bot.sh: сторож, удержание, фиксация и откат — сценарии ревью этапа 0–1.

- откат набора не удаляет текущий код под неполный прежний; удержание
  снимается только после удачного переноса (guard-1);
- источник отката ниже версии совместимости — источника нет; переход через
  неё снимает удержание (guard-2);
- удержание привязано к id хода или команды: другой id и ручной update его
  снимают (guard-3);
- откат пояса — раскомментированием строки, правки app.yaml остаются (guard-4);
- откат из ExecStartPre: метка и пояс — сразу, окружение — потом (guard-6);
- уборка окружений не трогает прежнее окружение взведённого сторожа (guard-7);
- под замком установки pid апдейтера — в update-running (logic-2).

Скрипт настоящий целиком (tests/unit/test_update_guard.py, _Host).
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests.unit.test_update_guard import _Host, _age, _guard_state, _tree

pytestmark = pytest.mark.unit


def _run(host: _Host, *args: str, hold: str = "") -> subprocess.CompletedProcess:
    env = {"PATH": f"{host.bin}:/usr/bin:/bin:/usr/sbin:/sbin", "CALLS": str(host.calls), "RUN_RC": "0",
           "TIMER_RC": "3", "AWG_UPDATE_PREV": str(host.inst / ".prev-code"), "HOME": str(host.tmp)}
    if hold:
        env["AWG_UPDATE_HOLD"] = hold
    return subprocess.run(["bash", str(host.script), *args], capture_output=True, text=True, timeout=30, env=env)


def _held(tmp_path, run_id: str = "a1b2c3d4") -> _Host:
    host = _Host(tmp_path)
    assert _run(host, "__post_update", "0", hold=run_id).returncode == 0
    assert _guard_state(host)["HOLD"] == run_id, "удержание без id хода"
    host.mark(host.new)
    host.guard("--alive")
    hold = dict(ln.split("=", 1) for ln in (host.data / "update-hold").read_text(encoding="utf-8").splitlines())
    assert hold["RUN"] == run_id
    return host


def _next_step(host: _Host, version: str, tag: str) -> None:
    """Следующее обновление поверх удержания: новый код на месте, прежний — в .prev-code."""
    prev = host.inst / ".prev-code"
    prev.mkdir()
    _tree(prev, host.new, "3.4.0", "new")
    _tree(host.inst, version, "3.4.0", tag)


def test_a_revert_onto_an_incomplete_old_code_keeps_the_current_one(tmp_path):
    host = _held(tmp_path)
    os.remove(host.inst / ".hold-code" / "awgbot" / "__main__.py")
    r = host.run("__update_revert")
    assert r.returncode == 1 and "неполон" in r.stderr
    assert host.whose() == "new" and (host.inst / "awgbot" / "__main__.py").exists(), "хост остался без кода"
    assert (host.inst / ".hold-code").is_dir() and (host.data / "update-hold").exists(), "удержание снято"
    assert host.log()[-1] == "systemctl start awg-bot"


def test_a_source_below_the_compat_floor_is_no_source(tmp_path):
    host = _held(tmp_path)
    _tree(host.inst / ".hold-code", "3.3.1", "3.3.0", "old")
    assert host.run("__update_revert", "--check").returncode == 2
    assert host.run("__update_revert").returncode == 2 and host.whose() == "new"


def test_crossing_the_floor_drops_the_hold(tmp_path):
    host = _held(tmp_path)
    _next_step(host, "3.5.0", "third")
    _tree(host.inst / ".prev-code", host.new, "3.4.0", "new")
    _tree(host.inst, "3.5.0", "3.5.0", "third")           # новый код поднял версию совместимости выше прежней
    assert _run(host, "__post_update", "0", hold="b1b2c3d4").returncode == 0
    assert not (host.inst / ".hold-code").exists() and not (host.data / "update-hold").exists()


def test_a_step_of_another_run_drops_the_old_hold(tmp_path):
    host = _held(tmp_path, "a1b2c3d4")
    _next_step(host, "3.4.2", "third")
    assert _run(host, "__post_update", "0", hold="b1b2c3d4").returncode == 0
    host.mark("3.4.2")
    host.guard("--alive")
    hold = dict(ln.split("=", 1) for ln in (host.data / "update-hold").read_text(encoding="utf-8").splitlines())
    assert hold["RUN"] == "b1b2c3d4" and hold["PREV"] == host.new, "удержание прошлого хода пережило новый"
    assert (host.inst / ".hold-code" / "whose").read_text(encoding="utf-8") == "new"


def test_a_step_of_the_same_run_keeps_the_very_first_old_one(tmp_path):
    host = _held(tmp_path, "a1b2c3d4")
    _next_step(host, "3.4.2", "third")
    assert _run(host, "__post_update", "0", hold="a1b2c3d4").returncode == 0
    host.mark("3.4.2")
    host.guard("--alive")
    assert (host.inst / ".hold-code" / "whose").read_text(encoding="utf-8") == "old"


def test_a_manual_update_drops_the_hold(tmp_path):
    host = _held(tmp_path)
    _next_step(host, "3.4.2", "third")
    assert _run(host, "__post_update", "0").returncode == 0
    assert not (host.inst / ".hold-code").exists() and not (host.data / "update-hold").exists()


def test_the_timezone_comes_back_as_a_line_and_other_edits_stay(tmp_path):
    host = _Host(tmp_path)
    host.post_update()
    host.app.write_text(host.app.read_text(encoding="utf-8") + "extra: 1\n", encoding="utf-8")  # правка в окне
    _age(host, 11)
    host.guard()
    text = host.app.read_text(encoding="utf-8")
    assert 'timezone: "Europe/Moscow"' in text and "# timezone:" not in text
    assert "extra: 1" in text, "откат пояса затёр правку app.yaml"


def test_the_start_check_writes_the_mark_and_leaves_the_env_for_later(tmp_path):
    host = _Host(tmp_path)
    host.post_update()
    _age(host, 11)
    host.guard("--start", run_rc=1)
    assert (host.data / "update-rolled-back").exists() and host.venv() == host.env_old
    assert host.env_new.is_dir(), "окружение удаляется из ExecStartPre (таймаут 90 с)"


def test_pruning_keeps_the_old_env_of_an_armed_guard(tmp_path):
    host = _held(tmp_path)
    _next_step(host, "3.4.2", "third")
    env_third = host.inst / ".venvs" / "C"
    env_third.mkdir()
    (host.inst / "venv").unlink()
    (host.inst / "venv").symlink_to(env_third)
    (host.inst / ".venvs" / ".prev").write_text(f"{host.env_new}\n", encoding="utf-8")
    assert _run(host, "__post_update", "0", hold="a1b2c3d4").returncode == 0     # сторож взведён
    assert host.run("__update_commit").returncode == 0                            # фиксация прошлого шага
    assert host.env_new.is_dir(), "уборка сняла окружение, на которое откатит сторож"
    assert (host.inst / ".venvs" / ".prev").exists()


def test_the_updater_leaves_its_pid_under_the_lock(tmp_path):
    host = _held(tmp_path)
    pid = (host.data / "update-running").read_text(encoding="utf-8").split()[0]
    assert pid.isdigit() and int(pid) > 1


@pytest.mark.skipif(shutil.which("flock") is None, reason="нет flock")
def test_a_second_update_while_one_holds_the_lock_is_refused(tmp_path):
    host = _Host(tmp_path)
    lock = host.data / "update-guard.lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "5"])
    try:
        import time
        time.sleep(0.3)
        r = host.run("update", str(tmp_path / "none.tgz"))
        assert r.returncode != 0 and "уже идёт обновление" in r.stdout + r.stderr
    finally:
        holder.kill()
