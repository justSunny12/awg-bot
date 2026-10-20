"""awg-bot.sh: сторож, удержание, фиксация и откат — сценарии ревью этапа 0–1.

- откат набора не удаляет текущий код под неполный прежний; удержание
  снимается только после удачного переноса (guard-1);
- источник отката ниже версии совместимости — источника нет; переход через
  неё снимает удержание (guard-2);
- удержание привязано к id хода или команды: другой id и ручной update его
  снимают (guard-3);
- откат из ExecStartPre: метка и пояс — сразу, окружение — потом (guard-6);
- уборка окружений не трогает прежнее окружение взведённого сторожа (guard-7);
- под замком установки pid апдейтера — в update-running (logic-2).

Скрипт настоящий целиком (tests/unit/test_update_guard.py, _Host).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess

import pytest

from tests.botscript import flat

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
    """Пока шаг обновления ждёт факта, в update-running — pid процесса под
    замком (бот по нему видит живой апдейтер); отработавший сторож pid за собой
    убирает: мёртвый pid мог бы достаться новому процессу."""
    from tests.unit.test_update_guard import _func
    host = _Host(tmp_path)
    # сам замок: пока процесс под ним — в файле его pid
    prog = ("set -euo pipefail\n" f'DATA_DIR="{host.data}"; UPDATE_LOCK="{host.data}/update-guard.lock"\n'
            + _func(flat(), "update_lock") + "\n"
            + 'update_lock -n; cat "$DATA_DIR/update-running"; echo "me $$"\n')
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    pid, me = r.stdout.split()[0], r.stdout.split()[-1]
    assert pid.isdigit() and pid == me, r.stdout
    # отработавший сторож (шаг набора с удержанием) pid за собой убирает
    host.post_update(hold=True)
    host.mark(host.new)
    host.guard("--alive")
    assert not (host.data / "update-running").exists(), "pid отработавшего сторожа остался"


@pytest.mark.skipif(shutil.which("flock") is None, reason="нет flock")
def test_a_second_update_while_one_holds_the_lock_is_refused(tmp_path):
    """Второй апдейтер (повтор шага хода, ручной поверх кнопки), пока первый
    держит замок установки, не начинает — иначе два переноса кода в одно /opt.
    Замок берётся после проверки «установлено» (venv/bin/python), поэтому
    хост — с окружением, как настоящая установка."""
    import time
    host = _Host(tmp_path)
    py = host.env_new / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8"); py.chmod(0o755)
    lock = host.data / "update-guard.lock"
    ready = tmp_path / "held"
    # держатель — своей группой: замок у дочернего sleep, не у самого flock
    holder = subprocess.Popen(["flock", str(lock), "sh", "-c", f'touch "{ready}"; exec sleep 30'],
                              start_new_session=True)
    try:
        for _ in range(100):                         # ждём факта: замок взят
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "держатель замка не взял его"
        r = host.run("update", str(tmp_path / "none.tgz"))
        out = r.stdout + r.stderr
        assert r.returncode != 0 and "уже идёт обновление" in out, out
        assert "не найден awg-bot.tgz" not in out, "второй апдейтер пошёл дальше замка"
    finally:
        os.killpg(holder.pid, signal.SIGKILL)
        holder.wait()
