"""seam.py — часть пакета awg (см. __init__): шов контейнер/хост — запуск команд
(`_run`, `_exec*`), заслон host-режима, чтение и запись файлов сервера."""
from __future__ import annotations

import subprocess
from typing import Optional

from awgbot.core import config
from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде

from .common import AwgError


# ─────────────────────────────────────────────────────────────────────────────
# Низкоуровневый запуск docker exec
# ─────────────────────────────────────────────────────────────────────────────

def _run(
    args: list[str],
    input_data: Optional[bytes] = None,
    timeout: int = 15,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Запускает произвольную команду (list, без shell). Возвращает CompletedProcess.
    При check=True и ненулевом коде — AwgError с stderr."""
    try:
        proc = subprocess.run(
            args,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise AwgError(f"Команда не найдена: {args[0]} ({e})")
    except subprocess.TimeoutExpired:
        raise AwgError(f"Таймаут команды: {' '.join(args[:4])}...")
    if check and proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise AwgError(f"Ошибка команды {' '.join(args[:5])}: {err}")
    return proc


def in_container() -> bool:
    """Ходим ли мы в контейнер, или команды идут прямо на хосте.

    Функция, а не константа модуля: значение замёрзло бы на импорте, и тест,
    подменяющий config.AWG_RUNTIME, проверял бы не то, что выполняется в бою.
    """
    return config.AWG_RUNTIME == "docker"


class HostModeUnsupported(RuntimeError):
    """Операция ещё не портирована на host-режим (docs/ROADMAP.md, шаг 2).

    НЕ наследник AwgError, и это намеренно. Половина вызывающих глотает AwgError
    и продолжает с пустым результатом: container_pid → None, awg_responding →
    False, host_ssh_targets → частичный список. Унаследуй заслон от AwgError — и
    он утонул бы ровно в тех except'ах, ради которых поставлен, вернув ту самую
    молчаливую деградацию. Для SSH-фильтра это была бы дыра без единого признака.
    """


def _docker_only(what: str) -> None:
    """Заслон для того, что в host-режиме ещё не портировано."""
    if not pkg.in_container():
        raise HostModeUnsupported(
            f"{what}: в host-режиме не поддерживается — перенос ещё не сделан "
            f"(docs/ROADMAP.md, шаг 2)")


def _exec(cont_args: list[str], **kw) -> subprocess.CompletedProcess:
    """Команда на сервере awg: `docker exec <container> ...` или прямо на хосте.

    Единственный вход внутрь — через неё ходят и блокировки, и правка конфига, и
    условная маршрутизация (infra/routing/base.py::_cont). Поэтому переезд с
    контейнера на хост здесь и переключается, а не в двух десятках мест.
    """
    if pkg.in_container():
        return pkg._run(["docker", "exec", config.CONTAINER, *cont_args], **kw)
    return pkg._run(list(cont_args), **kw)


def _exec_i(cont_args: list[str], input_data: bytes, **kw) -> subprocess.CompletedProcess:
    """То же с подачей stdin."""
    if pkg.in_container():
        return pkg._run(
            ["docker", "exec", "-i", config.CONTAINER, *cont_args],
            input_data=input_data, **kw,
        )
    return pkg._run(list(cont_args), input_data=input_data, **kw)


def _exec_sh(script: str, **kw) -> subprocess.CompletedProcess:
    """`sh -c '<script>'` — для пайпов и редиректов.
    script собирается ТОЛЬКО из констант конфига и валидированных значений."""
    return pkg._exec(["sh", "-c", script], **kw)


# ─────────────────────────────────────────────────────────────────────────────
# Чтение / запись файлов контейнера (root в контейнере игнорирует права файлов)
# ─────────────────────────────────────────────────────────────────────────────

def read_file(path: str) -> str:
    return pkg._exec(["cat", path]).stdout.decode(errors="replace")


def write_file(path: str, content: str) -> None:
    """Пишет файл целиком через stdin (без heredoc → без риска инъекций).
    `cat > file` усекает существующий файл, сохраняя inode и права."""
    # Путь в кавычках. Он приходит из констант конфига, но конфиг — это yaml,
    # который правят руками: пробел или точка с запятой в awg_dir превратились бы
    # в исполнение произвольной команды от root. Кавычки стоят ноль.
    pkg._exec_i(["sh", "-c", f'cat > "{path}"'], input_data=content.encode())
