"""
Атомарная запись файла с секретом (util/fsatomic).

Стережём:
- после подмены имени — fsync каталога: без него при потере питания после
  replace файл возвращался прежним или пропадал вовсе (новый env, конфиг
  линка, ключ копии);
- байтовый вариант (архив копии): 0600 с первого байта, а не write_bytes и
  chmod после — окно с правами по umask; сбой записи (ENOSPC) не оставляет
  ни обрезанного файла, ни временного рядом, прежний файл цел.
"""
from __future__ import annotations

import os
import stat

import pytest

from awgbot.util import fsatomic

pytestmark = pytest.mark.unit


def _spy_fsync(monkeypatch):
    kinds = []
    real = os.fsync

    def spy(fd):
        kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real(fd)
    monkeypatch.setattr(os, "fsync", spy)
    return kinds


@pytest.mark.parametrize("write,payload", [(fsatomic.write_private, "A=1\n"),
                                           (fsatomic.write_private_bytes, b"\x00blob")])
def test_the_directory_is_synced_after_the_replace(tmp_path, monkeypatch, write, payload):
    kinds = _spy_fsync(monkeypatch)
    write(tmp_path / "env", payload)
    assert kinds == ["file", "dir"], kinds


def test_bytes_are_private_from_the_first_byte(tmp_path, monkeypatch):
    modes = []
    real = os.replace

    def spy(src, dst):
        modes.append(stat.S_IMODE(os.stat(src).st_mode))
        return real(src, dst)
    monkeypatch.setattr(os, "replace", spy)
    dst = tmp_path / "copy.tgz.enc"
    fsatomic.write_private_bytes(dst, b"x" * 1000)
    assert dst.read_bytes() == b"x" * 1000
    assert modes == [0o600] and stat.S_IMODE(dst.stat().st_mode) == 0o600


def test_a_failed_bytes_write_leaves_the_old_file_and_no_tmp(tmp_path, monkeypatch):
    dst = tmp_path / "copy.tgz.enc"
    dst.write_bytes(b"old")
    real = os.fsync

    def boom(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(28, "No space left on device")
        return real(fd)
    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        fsatomic.write_private_bytes(dst, b"new" * 100)
    assert dst.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["copy.tgz.enc"]
