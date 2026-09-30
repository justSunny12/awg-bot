"""systemctl одним способом: живость и автозагрузка юнита."""
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
