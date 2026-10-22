"""`confirm` в awg-bot.sh без терминала.

Апдейтер запускает `awg-bot` под systemd-run — терминала у него нет, и
`read … < /dev/tty` падал с «/dev/tty: No such device or address» в журнал
сервиса, а ответом становилось пустое значение (то есть «n» и для вопросов с
умолчанием «y»). Без терминала функция берёт умолчание молча; с терминалом
по-прежнему спрашивает.
"""
from __future__ import annotations

import fcntl
import os
import select
import subprocess
import termios

import pytest

from tests.botscript import flat

pytestmark = pytest.mark.unit


def _bash() -> str:
    """bash не старше 4 (`${a,,}` в confirm): на macOS системный — 3.2, тест
    там пропускается; на Linux-стенде и хостах поставки — bash 5."""
    for cand in ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        try:
            v = subprocess.run([cand, "-c", "echo ${BASH_VERSINFO[0]}"], text=True, capture_output=True, timeout=10)
        except OSError:
            continue
        if v.returncode == 0 and v.stdout.strip().isdigit() and int(v.stdout.strip()) >= 4:
            return cand
    pytest.skip("нет bash ≥ 4 — confirm использует ${a,,}")


def _confirm_src() -> str:
    text = flat()
    start = text.index("confirm() {")
    return text[start:text.index("\n}\n", start) + 2]


def _run_without_tty(script: str) -> subprocess.CompletedProcess:
    """bash в новой сессии: управляющего терминала нет, /dev/tty не открывается."""
    return subprocess.run([_bash(), "-c", _confirm_src() + "\n" + script], text=True, capture_output=True,
                          stdin=subprocess.DEVNULL, start_new_session=True, timeout=20)


@pytest.fixture(scope="module")
def no_tty():
    probe = _run_without_tty(": </dev/tty && echo open")
    if "open" in probe.stdout:
        pytest.skip("в этой среде /dev/tty открывается и без терминала — умолчание не проверить")


@pytest.mark.parametrize("default, rc", [("y", 0), ("n", 1)])
def test_without_a_terminal_confirm_takes_the_default_silently(no_tty, default, rc):
    r = _run_without_tty(f'confirm "Снять?" {default}; echo rc=$?')
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"rc={rc}", f"умолчание «{default}» не стало ответом: {r.stdout!r}"
    assert r.stderr == "", "без терминала confirm шумит в журнал: " + r.stderr


def test_without_a_terminal_an_omitted_default_means_no(no_tty):
    r = _run_without_tty('confirm "Снять?"; echo rc=$?')
    assert r.stdout.strip() == "rc=1" and r.stderr == "", r.stdout + r.stderr


def test_with_a_terminal_confirm_still_asks_and_honours_the_answer():
    """Терминал есть — вопрос задаётся на нём и ответ важнее умолчания."""
    bash = _bash()
    try:
        master, slave = os.openpty()
    except OSError:
        pytest.skip("pty недоступен")
    def _own_tty():                                      # новая сессия, управляющий терминал — пара pty
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    try:
        p = subprocess.Popen([bash, "-c", _confirm_src() + '\nconfirm "Снять?" y; echo rc=$?'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, preexec_fn=_own_tty)
        os.write(master, b"n\n")                        # ответ ждёт в буфере pty до read
        out, err = p.communicate(timeout=20)
        echoed = ""
        if select.select([master], [], [], 2)[0]:
            echoed = os.read(master, 4096).decode(errors="replace")
    finally:
        os.close(slave)
        os.close(master)
    assert out.strip() == "rc=1", f"ответ «n» с терминала не перебил умолчание «y»: {out!r} {err!r}"
    assert "Снять? [Y/n]" in err, f"вопрос не задан (read -p пишет его в stderr): {err!r}"
    assert echoed.startswith("n"), f"ответ читался не с терминала: {echoed!r}"
