"""
fsatomic.py — запись файла с секретом: временный файл рядом, 0600 с первого
байта, fsync, атомарная подмена. Усечение с последующей записью (open("w"))
при ENOSPC или падении оставляло пустой /etc/awg-bot/env — и основной бот не
стартовал. Одна реализация на обе роли.
"""
from __future__ import annotations

import os
from pathlib import Path


def write_private(path, text: str, mode: int = 0o600) -> None:
    """Записать text в path атомарно (tmp + replace), с правами mode."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


__all__ = ["write_private"]
