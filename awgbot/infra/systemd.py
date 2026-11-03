"""systemctl одним способом: живость и автозагрузка юнита, остановка,
перезапуск, включение и снятие с автозагрузки, сброс неудачи, перечитывание
юнитов и наличие файла юнита. Пуска здесь нет: юниты гостя и процесса открытия
машина резерва запускает сама (тесты наблюдают запуск подменой в её модулях).

Остановка, перезапуск, enable/disable, reset-failed и daemon-reload
бросают исключения ОС и subprocess как есть — вызывающий решает, отказ это
или строка в журнал (`except (OSError, subprocess.SubprocessError)`). restart, enable, disable и
daemon_reload отдают итог целиком (код и вывод): причину отказа — хвост
stderr — вызывающие показывают человеку. Потолок задаётся всегда: зависший на
D-Bus systemctl иначе держал бы поток бота без предела.
"""
from __future__ import annotations

import subprocess
from typing import Optional


def is_active(unit: str, timeout: int = 10) -> Optional[bool]:
    """Жив ли юнит; None — systemctl недоступен или не ответил."""
    try:
        proc = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                              capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def enabled_state(unit: str, timeout: int = 5) -> str:
    """Вывод `systemctl is-enabled` (enabled/disabled/not-found/…); пусто —
    systemctl недоступен."""
    try:
        proc = subprocess.run(["systemctl", "is-enabled", unit],
                              capture_output=True, timeout=timeout)
        return proc.stdout.decode(errors="replace").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _call(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, timeout=timeout)


def _ctl(verb: str, unit: str, timeout: float) -> int:
    return _call([verb, unit], timeout).returncode


def stop(unit: str, timeout: float = 60) -> int:
    """`systemctl stop` — код возврата (юнита нет — не отказ); нет systemctl
    или завис — исключение как есть."""
    return _ctl("stop", unit, timeout)


def reset_failed(unit: str, timeout: float = 30) -> int:
    """`systemctl reset-failed` — код возврата (юнит не в failed — не отказ);
    нет systemctl или завис — исключение как есть."""
    return _ctl("reset-failed", unit, timeout)


def restart(unit: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """`systemctl restart` — итог как есть (ненулевой код — не исключение:
    причину в stderr разбирает вызывающий); нет systemctl или завис —
    OSError / TimeoutExpired как есть."""
    return _call(["restart", unit], timeout)


def enable(unit: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """`systemctl enable` — итог как есть; нет systemctl или завис — исключение."""
    return _call(["enable", unit], timeout)


def disable(unit: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """`systemctl disable` — итог как есть; нет systemctl или завис — исключение."""
    return _call(["disable", unit], timeout)


def daemon_reload(timeout: float = 30) -> subprocess.CompletedProcess:
    """`systemctl daemon-reload` — итог как есть; нет systemctl или завис —
    исключение."""
    return _call(["daemon-reload"], timeout)


def unit_file_listed(unit: str, timeout: float = 30) -> bool:
    """Файл юнита известен systemd (`list-unit-files <юнит>`: код 0 и имя в
    выводе); нет systemctl или завис — исключение как есть."""
    proc = _call(["list-unit-files", unit], timeout)
    return proc.returncode == 0 and unit.encode() in (proc.stdout or b"")


__all__ = ["is_active", "enabled_state", "start", "stop", "reset_failed", "restart", "enable", "disable",
           "daemon_reload", "unit_file_listed"]
