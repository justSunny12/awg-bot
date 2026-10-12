"""server.py — часть пакета awg (см. __init__): статистика пиров, статус контейнера
и сервиса, каталог для вотчдога, перезапуск сервера."""
from __future__ import annotations

import time as _time
from typing import Optional

from awgbot.core import config
from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде

from .common import AwgError


# ─────────────────────────────────────────────────────────────────────────────
# Статистика
# ─────────────────────────────────────────────────────────────────────────────

def show_dump(iface: Optional[str] = None) -> list[dict]:
    """awg show <if> dump → распарсенный список пиров.

    Единственное место, где интерфейс нужен по существу: счётчики и хендшейки
    живут в ядре по интерфейсам, и опрос одного не увидит пиров другого."""
    out = pkg._exec(["awg", "show", pkg.iface_of(iface), "dump"]).stdout.decode(errors="replace")
    return pkg.parse_dump(out)


# ─────────────────────────────────────────────────────────────────────────────
# Статус контейнера / мониторинг
# ─────────────────────────────────────────────────────────────────────────────

def _inspect(fmt: str) -> str:
    pkg._docker_only("docker inspect")
    return pkg._run(
        ["docker", "inspect", "-f", fmt, config.CONTAINER]
    ).stdout.decode().strip()


def container_pid() -> Optional[int]:
    """PID главного процесса контейнера на хосте (для inotify через /proc/<PID>/root).
    Меняется при рестарте контейнера."""
    try:
        pid = pkg._inspect("{{.State.Pid}}")
        return int(pid) if pid and pid != "0" else None
    except (AwgError, ValueError):
        return None


def container_started_at() -> Optional[str]:
    """StartedAt контейнера (ISO). Для детекта рестарта → реконсиляция блокировок."""
    try:
        return pkg._inspect("{{.State.StartedAt}}") or None
    except AwgError:
        return None


def awg_responding() -> bool:
    """awg show awg0 отвечает без ошибки = демон жив."""
    try:
        pkg._exec(["awg", "show", config.AWG_INTERFACE], check=True)
        return True
    except AwgError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# То же самое без оглядки на способ запуска
# ─────────────────────────────────────────────────────────────────────────────

def watch_root() -> Optional[str]:
    """Каталог с файлами awg, каким его видит бот — для inotify.

    В контейнере файлы лежат в чужом mount namespace, добраться до них можно
    только через /proc/<PID>/root, и PID меняется при каждом рестарте. На хосте
    это просто AWG_DIR, который никуда не переезжает, — поэтому и вотчдог
    привязывается к ПУТИ, а не к PID: путь есть в обоих режимах, PID только в
    одном.
    """
    if not pkg.in_container():
        return config.AWG_DIR
    pid = pkg.container_pid()
    return f"/proc/{pid}/root{config.AWG_DIR}" if pid else None


def _boot_time_iso() -> Optional[str]:
    """Время загрузки системы в формате StartedAt (UTC, суффикс Z)."""
    try:
        with open("/proc/stat", "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("btime "):
                    ts = int(line.split()[1])
                    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(ts))
    except (OSError, ValueError, IndexError):
        return None
    return None


def service_started_at() -> Optional[str]:
    """Когда стартовало то, что держит наши правила iptables.

    В контейнере это StartedAt: его рестарт обнуляет и DROP'ы блокировок, и
    SSH-цепочку. На хосте — время загрузки системы, потому что наши цепочки
    живут в хостовых таблицах и переживают всё, кроме перезагрузки: ни
    `awg-quick down/up`, ни рестарт бота их не трогают.

    Сигнал разный, смысл один: «состояние, которое мы поддерживаем, обнулилось —
    переналожи». Взять на хосте момент подъёма интерфейса было бы неверно: он
    меняется чаще, чем состояние теряется, и гонял бы реконсиляцию впустую.
    """
    return pkg.container_started_at() if pkg.in_container() else pkg._boot_time_iso()


def restart_server() -> None:
    """Перезапуск сервиса AmneziaWG (ТЗ 9.3).

    ВНИМАНИЕ: в docker-режиме после этого слетают iptables-правила внутри
    контейнера → нужна реконсиляция блокировок. На хосте они не слетают, но
    реконсиляция всё равно безвредна и делается в вызывающем.
    """
    if pkg.in_container():
        pkg._run(["docker", "restart", config.CONTAINER], timeout=60)
        return
    # down может честно не сработать, если интерфейса нет, — это не ошибка,
    # ради которой стоит не поднимать его обратно.
    pkg._run(["awg-quick", "down", config.AWG_INTERFACE], check=False, timeout=60)
    pkg._run(["awg-quick", "up", config.AWG_INTERFACE], timeout=60)
