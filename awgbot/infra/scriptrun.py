"""Скрипт обвязки одним способом: запуск с потолком, вывод одним текстом,
отказ — исключением класса владельца с хвостом вывода.

Пять обёрток скриптов install/ (зона резерва, зонды кворума, линк шлюза к
резерву, резолвер пользователей и резолвер админа) держали по копии запуска;
ошибки у каждой свои — класс и имя скрипта для «не запустился» передаются, как
в infra/nftjson.run.
"""
from __future__ import annotations

import os
import subprocess
from typing import Optional


def run_script(argv: list[str], *, error: type[Exception], what: str, timeout: float,
               env: Optional[dict] = None, stdin: Optional[str] = None,
               tail: int = 3, check: bool = True) -> str:
    """argv (интерпретатор, скрипт, режим, аргументы) с окружением процесса и
    добавкой env; stdin — текст на вход (секреты — только так, не в argv).
    Возвращает stdout+stderr одним текстом без пробелов по краям.

    Не запустился (нет интерпретатора, ошибка ОС) или не уложился в timeout —
    error(f"{what} не запустился: …"). Ненулевой код при check — error с
    последними tail строками вывода (вывода нет — «код N»); check=False —
    код — состояние (режим status), текст возвращается как есть."""
    full_env = {**os.environ, **{k: str(v) for k, v in (env or {}).items()}}
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, env=full_env,
                              input=(stdin.encode() if stdin is not None else None))
    except (OSError, subprocess.SubprocessError) as e:
        raise error(f"{what} не запустился: {e}") from e
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    if check and proc.returncode != 0:
        raise error("\n".join(out.splitlines()[-tail:]) or f"код {proc.returncode}")
    return out


__all__ = ["run_script"]
