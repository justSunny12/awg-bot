"""Запуск команды ОТДЕЛЬНО от процесса бота.

Транзиентный юнит systemd-run вне нашего cgroup: `systemctl restart/stop`
бота иначе убил бы команду посреди дела (обновление, восстановление,
перезапуск самого сервиса). Без systemd-run — новая сессия, best effort.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time


def spawn_detached(argv: list[str], *, unit_prefix: str, env: dict[str, str] | None = None) -> None:
    """argv — команда; unit_prefix — имя юнита (уникальный хвост добавляется:
    повторный запуск не упадёт об «unit already exists»); env — добавка к
    окружению команды."""
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 close_fds=True)
    if shutil.which("systemd-run"):
        unit = f"{unit_prefix}-{int(time.time())}-{os.getpid()}"
        setenv = [f"--setenv={k}={v}" for k, v in (env or {}).items()]
        subprocess.Popen(["systemd-run", "--collect", "--quiet", *setenv, f"--unit={unit}", *argv], **quiet)
    else:
        subprocess.Popen(argv, start_new_session=True,
                         env={**os.environ, **env} if env else None, **quiet)
