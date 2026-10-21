"""
rsv.py — часть пакета gwguard (см. __init__): постоянный линк шлюза к резерву
`awgrsv` — обёртка install/reserve-gw-setup.sh, состояние пересылки, счётчики.

Скрипт ведёт конфиг линка (из раздела `reserve` документа конфигурации),
юнит `awg-rsv-gw.service`, свою таблицу nft `inet awg_rsv_guard` (вход с
линка закрыт, пересылка — по набору `rsv_open4`, ответы — по connmark).
Агент решает, когда открывать пересылку (слово сервера `state = away` или
`serving` резерва вместе с фактом записи DNS), и зовёт `open`/`close`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from awgbot.core import config

RSV_IF = "awgrsv"
CONF_PATH = Path("/etc/awg-gw") / f"{RSV_IF}.conf"
STATE_PATH = Path("/var/lib/awg-gw/rsv.status")
SCRIPT_TIMEOUT = 120


class RsvError(RuntimeError):
    """Скрипт линка к резерву отказал; текст — его последние строки."""


def _script() -> Path:
    return config.BASE_DIR / "install" / "reserve-gw-setup.sh"


def run(mode: str, *args: str, env: dict | None = None, timeout: int = SCRIPT_TIMEOUT) -> str:
    argv = ["sh", str(_script()), f"--{mode.lstrip('-')}", *args]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, env={**os.environ, **(env or {})})
    except (OSError, subprocess.SubprocessError) as e:
        raise RsvError(f"скрипт линка к резерву не запустился: {e}") from e
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RsvError("\n".join(out.splitlines()[-3:]) or f"код {proc.returncode}")
    return out


def _kv(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def status() -> dict:
    """{link: up|down, open: 0|1, src, rx, tx, conf: present|absent}; скрипт
    не отвечает — всё пусто."""
    try:
        return _kv(run("status", timeout=30))
    except RsvError:
        return {}


def present() -> bool:
    return CONF_PATH.exists()


def up(conf_text: str, admin_ips: list[str] | None = None) -> str:
    """Конфиг линка → на диск и вверх (повтор того же — без изменений)."""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="awgrsv-", suffix=".conf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conf_text if conf_text.endswith("\n") else conf_text + "\n")
        os.chmod(tmp, 0o600)
        return run("up", tmp, env={"ADMIN_IPS": ",".join(admin_ips or [])})
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def reassert() -> str:
    return run("reassert")


def open_forward(sources: list[str]) -> str:
    srcs = [s for s in sources if s]
    if not srcs:
        return close_forward()
    return run("open", ",".join(srcs))


def close_forward() -> str:
    return run("close")


def down() -> str:
    return run("down")


def remove() -> str:
    return run("remove")


def is_open() -> bool:
    """Открыта ли пересылка — по файлу состояния скрипта (его пишет --open/--close,
    0644 без секретов): такт зовёт это каждые 5 с, скрипт ради строки не нужен."""
    try:
        for line in STATE_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("open="):
                return line[5:].strip() == "1"
    except OSError:
        pass
    return False


def counters() -> tuple[int, int] | None:
    """(rx, tx) линка; линка нет или awg не ответил — None."""
    try:
        proc = subprocess.run(["awg", "show", RSV_IF, "transfer"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    rx = tx = 0
    for line in proc.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            rx += int(parts[1])
            tx += int(parts[2])
    return rx, tx
