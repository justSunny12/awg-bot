"""seam.py — часть пакета awg (см. __init__): запуск команд на хосте
(`_run`, `_exec*`), чтение и запись файлов сервера."""
from __future__ import annotations

import subprocess
from typing import Optional

from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде

from .common import AwgError


# ─────────────────────────────────────────────────────────────────────────────
# Низкоуровневый запуск команд
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


def _exec(args: list[str], **kw) -> subprocess.CompletedProcess:
    """Команда на сервере awg — прямо на хосте. Единственный вход: через неё
    ходят и блокировки, и правка конфига, и условная маршрутизация."""
    return pkg._run(list(args), **kw)


def _exec_i(args: list[str], input_data: bytes, **kw) -> subprocess.CompletedProcess:
    """То же с подачей stdin."""
    return pkg._run(list(args), input_data=input_data, **kw)


def _exec_sh(script: str, **kw) -> subprocess.CompletedProcess:
    """`sh -c '<script>'` — для пайпов и редиректов.
    script собирается ТОЛЬКО из констант конфига и валидированных значений."""
    return pkg._exec(["sh", "-c", script], **kw)


# ─────────────────────────────────────────────────────────────────────────────
# Чтение / запись файлов сервера
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
