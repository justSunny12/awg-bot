"""uplinktxn.py — конфиг аплинка режимами обвязки --uplink / --uplink-commit / --uplink-revert и замок обвязки на всю транзакцию."""
from __future__ import annotations

import contextlib
import fcntl
import os
import time

from awgbot.core import config
from awgbot.infra import gwguard as pkg       # имена пакета — через него: их подменяют тесты
from .common import GwGuardError

APPLY_LOCK_PATH = "/run/awg-gw-apply.lock"
APPLY_LOCK_WAIT = 300           # с — столько же ждёт замка сам скрипт
UPLINK_TIMEOUT = 240            # с — stop/start юнита аплинка; замок уже наш, скрипт его не ждёт


@contextlib.contextmanager
def apply_lock(wait: float = APPLY_LOCK_WAIT):
    """Замок обвязки (тот же flock, что берёт скрипт) на всю транзакцию
    аплинка: ключи признания в юните, конфиг, проба, откат. Прогон юнита на
    загрузке иначе мог бы прочитать юнит до транзакции и записать его после
    неё со старыми ключами. Не дождались — GwGuardError."""
    path = pkg.APPLY_LOCK_PATH
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GwGuardError(f"обвязка занята другим прогоном дольше {int(wait)} с") from None
                time.sleep(1)
        yield
    finally:
        os.close(fd)                              # закрытие снимает flock


def _run(args: list[str], uplink_if: str, timeout: int) -> tuple[bool, str]:
    env = pkg._setup_env()
    env["UPLINK_IF"] = uplink_if
    env["AWG_GW_LOCK_HELD"] = "1"                 # замок держит агент (apply_lock)
    try:
        proc = pkg.subprocess.run([pkg.SETUP_SCRIPT, *args], capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return False, "отсутствует скрипт обвязки — «🚑 Восстановить»"
    except pkg.subprocess.TimeoutExpired:
        return False, f"скрипт обвязки не ответил за {timeout} с"
    except (OSError, pkg.subprocess.SubprocessError) as e:
        return False, str(e)
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    return proc.returncode == 0, out


def uplink_apply(path: str, uplink_if: str, timeout: int = UPLINK_TIMEOUT) -> tuple[bool, str]:
    """Поставить конфиг аплинка из файла режимом обвязки `--uplink`: белый
    список и Table = off проверяет скрипт, он же дописывает PostUp политики,
    кладёт копию прежнего в <аплинк>.conf.before, поднимает юнит
    awg-quick@<аплинк> и возвращает прежний, если новый не поднялся. Зовётся
    под apply_lock. (ok, вывод): в выводе строка `uplink: unchanged|installed`."""
    return _run(["--uplink", path], uplink_if, timeout)


def uplink_finish(uplink_if: str, commit: bool, timeout: int = UPLINK_TIMEOUT) -> tuple[bool, str]:
    """Итог пробы: commit — копию прежнего прочь; иначе прежний назад байт в
    байт (без белого списка — это файл с того же диска) и подъём аплинка.
    Копии нет — «unchanged», не ошибка. Зовётся под apply_lock."""
    return _run(["--uplink-commit" if commit else "--uplink-revert"], uplink_if, timeout)


def uplink_applied_how(out: str) -> str:
    """unchanged | installed | refused | committed | reverted | пусто — по строке «uplink: …»."""
    for line in reversed(out.splitlines()):
        if line.startswith("uplink: "):
            return line[8:].strip()
    return ""


def uplink_conf_path(uplink_if: str) -> str:
    return f"{config.GW_CONF_DIR}/{uplink_if}.conf"
