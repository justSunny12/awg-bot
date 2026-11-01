"""
fsatomic.py — атомарная запись файла: свой временный файл рядом (mkstemp —
0600 с первого байта, своё имя на каждый вызов), fsync, права до подмены,
replace, fsync каталога. Усечение с последующей записью (open("w")) при ENOSPC
или падении оставляло пустой /etc/awg-bot/env — и основной бот не стартовал;
общее имя `<файл>.tmp` у двух писателей одного файла давало подмену смесью двух
записей (или чужой недописанный файл). Одна реализация на обе роли.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

# права нового файла при mode=None: как у файла, созданного open() под umask 022 сервиса
NEW_FILE_MODE = 0o644


def write_atomic(path, data, mode: Optional[int] = None, fsync: bool = True) -> None:
    """Записать data (str — в UTF-8, или bytes) в path атомарно. mode — права
    файла; None — сохранить права прежнего файла (нового — NEW_FILE_MODE).
    fsync — данные и подмена имени на диске до возврата."""
    raw = data.encode("utf-8") if isinstance(data, str) else data      # не bytes — TypeError в write, прежний цел
    write_stream(path, lambda f: f.write(raw), mode, fsync=fsync)


def write_stream(path, fill: Callable[..., T], mode: Optional[int] = None, *, fsync: bool = True) -> T:
    """То же, что write_atomic, но содержимое пишет `fill(f)` в двоичный файл —
    потоком, без копии в памяти. Возвращает то, что вернул fill; сбой (и
    KeyboardInterrupt/SystemExit) — временный файл прочь, прежний на месте."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            got = fill(f)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.chmod(tmp, _target_mode(p, mode))
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if fsync:
        fsync_dir(p.parent)
    return got


def _target_mode(p: Path, mode: Optional[int]) -> int:
    if mode is not None:
        return mode
    try:
        return stat.S_IMODE(os.stat(p).st_mode)
    except FileNotFoundError:
        return NEW_FILE_MODE


def write_private(path, text: str, mode: int = 0o600) -> None:
    """Файл с секретом: text атомарно, с fsync, права mode (по умолчанию 0600)."""
    write_atomic(path, text, mode)


def write_private_bytes(path, raw: bytes, mode: int = 0o600) -> None:
    """Байтовый вариант write_private: архив копии, зашифрованный блоб."""
    write_atomic(path, raw, mode)


def fsync_dir(d) -> None:
    """Подмена имени живёт в каталоге: без его fsync при потере питания после
    replace файл мог вернуться прежним (или пропасть, если был новым)."""
    try:
        dfd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass                                    # ФС без fsync каталога — данные уже на месте
    finally:
        os.close(dfd)


__all__ = ["write_atomic", "write_stream", "write_private", "write_private_bytes", "fsync_dir", "NEW_FILE_MODE"]
