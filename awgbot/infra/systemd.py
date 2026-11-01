"""systemctl одним способом: живость и автозагрузка юнита, пуск, остановка и
сброс неудачи.

Пуск, остановка и reset-failed бросают исключения ОС и subprocess как есть —
вызывающий решает, отказ это или строка в журнал (`except (OSError,
subprocess.SubprocessError)`); start дополнительно считает отказом ненулевой
код. Потолок задаётся всегда: зависший на D-Bus systemctl иначе держал бы
поток бота без предела.
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


def _ctl(verb: str, unit: str, timeout: float) -> int:
    return subprocess.run(["systemctl", verb, unit], capture_output=True, timeout=timeout).returncode


def start(unit: str, timeout: float = 60) -> None:
    """`systemctl start`; ненулевой код — subprocess.SubprocessError «код N»,
    нет systemctl или завис — OSError / TimeoutExpired как есть."""
    rc = _ctl("start", unit, timeout)
    if rc != 0:
        raise subprocess.SubprocessError(f"код {rc}")


def stop(unit: str, timeout: float = 60) -> int:
    """`systemctl stop` — код возврата (юнита нет — не отказ); нет systemctl
    или завис — исключение как есть."""
    return _ctl("stop", unit, timeout)


def reset_failed(unit: str, timeout: float = 30) -> int:
    """`systemctl reset-failed` — код возврата (юнит не в failed — не отказ);
    нет systemctl или завис — исключение как есть."""
    return _ctl("reset-failed", unit, timeout)
