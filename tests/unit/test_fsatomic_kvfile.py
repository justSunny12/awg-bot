"""util/fsatomic и util/kvfile — запись файла с секретом и разбор KEY=VALUE.

Что стережём: write_private — файл с токеном (/etc/awg-bot/env, ключи) ни в
какой момент не читаем чужими (0600 с первого байта, а не chmod после записи),
падение посреди записи не оставляет ни пустого/обрезанного файла на месте
прежнего, ни временного рядом; kvfile.read — статус скриптов обвязки и
состояние ядра разбираются одинаково при комментариях, пустых строках и «=» в
значении, нет файла — пусто, а не исключение.

Цена ошибки: окно с 0644 — токен бота читает любой пользователь хоста;
усечённый env — бот не стартует после ENOSPC; «=» в значении, отрезанное
разбором, — неверный ключ или путь у скрипта. Параллельные записи одного
файла стережёт test_review_fixes_gw_store.
"""
from __future__ import annotations

import os
import stat

import pytest

from awgbot.util import fsatomic, kvfile


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


# ── write_private ───────────────────────────────────────────────────────────

def test_the_temporary_file_is_private_from_the_first_byte(tmp_path, monkeypatch):
    """Права смотрим на дескрипторе в момент fsync (байты уже записаны) и на
    временном файле в момент подмены — оба раза 0600, при любом umask."""
    seen = {}
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        seen["fsync"] = stat.S_IMODE(os.fstat(fd).st_mode)
        return real_fsync(fd)

    def replace(src, dst):
        seen["replace"] = (_mode(src), os.path.dirname(src) == str(tmp_path))
        return real_replace(src, dst)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    old = os.umask(0)
    try:
        fsatomic.write_private(tmp_path / "env", "BOT_TOKEN=DUMMY\n")
    finally:
        os.umask(old)
    assert seen == {"fsync": 0o600, "replace": (0o600, True)}, seen
    assert (tmp_path / "env").read_text(encoding="utf-8") == "BOT_TOKEN=DUMMY\n"
    assert _mode(tmp_path / "env") == 0o600


def test_the_mode_argument_is_applied_before_the_swap(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (seen.append(_mode(s)), real_replace(s, d))[1])
    fsatomic.write_private(tmp_path / "pub.conf", "x", mode=0o640)
    assert seen == [0o640] and _mode(tmp_path / "pub.conf") == 0o640


def test_an_existing_wider_file_becomes_private(tmp_path):
    p = tmp_path / "env"
    p.write_text("OLD=1\n", encoding="utf-8")
    os.chmod(p, 0o644)
    fsatomic.write_private(p, "NEW=1\n")
    assert _mode(p) == 0o600 and p.read_text(encoding="utf-8") == "NEW=1\n"


@pytest.mark.parametrize("where", ["write", "fsync", "replace"])
def test_a_failure_mid_write_keeps_the_old_file_and_leaves_no_tmp(tmp_path, monkeypatch, where):
    p = tmp_path / "env"
    p.write_text("BOT_TOKEN=OLD\n", encoding="utf-8")
    os.chmod(p, 0o600)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    if where == "fsync":
        monkeypatch.setattr(os, "fsync", boom)
    elif where == "replace":
        monkeypatch.setattr(os, "replace", boom)
    with pytest.raises((OSError, TypeError)):
        # «write»: не строка — падение в f.write, после mkstemp
        fsatomic.write_private(p, object() if where == "write" else "BOT_TOKEN=NEW\n")
    assert p.read_text(encoding="utf-8") == "BOT_TOKEN=OLD\n", "прежний файл задет"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["env"], "временный файл остался"


def test_an_interrupt_mid_write_also_removes_the_tmp(tmp_path, monkeypatch):
    """KeyboardInterrupt/SystemExit (остановка сервиса посреди записи) — не
    Exception, но временный файл всё равно убирается."""
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        fsatomic.write_private(tmp_path / "env", "X=1\n")
    assert list(tmp_path.iterdir()) == []


def test_the_parent_directory_is_created(tmp_path):
    p = tmp_path / "a" / "b" / "env"
    fsatomic.write_private(p, "X=1\n")
    assert p.read_text(encoding="utf-8") == "X=1\n"


# ── kvfile.read ─────────────────────────────────────────────────────────────

def test_missing_or_unreadable_file_is_empty(tmp_path):
    assert kvfile.read(tmp_path / "nope") == {}
    assert kvfile.read(tmp_path) == {}, "каталог вместо файла — пусто, без исключения"


def test_comments_blank_lines_and_equals_in_the_value(tmp_path):
    p = tmp_path / "state"
    p.write_text(
        "# шапка\n"
        "\n"
        "   \n"
        "A=1\n"
        "  B = два  \n"
        "   # отступленный комментарий\n"
        "URL=https://x/?a=1&b=2\n"
        "строка без знака\n"
        "EMPTY=\n"
        "Q=\"в кавычках\"\n"
        "A=2\n",
        encoding="utf-8")
    assert kvfile.read(p) == {"A": "2", "B": "два", "URL": "https://x/?a=1&b=2",
                              "EMPTY": "", "Q": "\"в кавычках\""}
    assert kvfile.read(p, strip_quotes=True)["Q"] == "в кавычках"
    assert list(kvfile.read(p)) == ["A", "B", "URL", "EMPTY", "Q"], "порядок — по первому появлению ключа"


def test_a_hash_inside_the_value_is_kept(tmp_path):
    """Комментарий — только целая строка с «#»: хвост «#…» в значении — часть значения."""
    p = tmp_path / "state"
    p.write_text("PSK=ab#cd\n", encoding="utf-8")
    assert kvfile.read(p) == {"PSK": "ab#cd"}
