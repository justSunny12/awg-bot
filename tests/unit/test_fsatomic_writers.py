"""util/fsatomic.write_atomic / write_stream / fsync_dir и писатели поверх них.

Что стережём: у каждой записи свой временный файл рядом (mkstemp), а не общее
имя `<файл>.tmp`; права — заданные (или, при mode=None, прежнего файла; нового —
0644) до подмены; сбой посреди записи — прежний файл цел, временного нет;
fsync=False — ни одного fsync. Писатели, ходившие своими копиями (журнал пары,
снимок базы основы на резерве, хранилище гостя, конфиг списков dnsmasq,
отметка жизни для сторожа обновления), — через это одно место.

Цена ошибки: два писателя одного файла с общим `.tmp` — второй усекал файл
первого посреди записи, подмена отдавала смесь двух записей или падала на
пропавшем временном; остаток прерванной записи на месте `.tmp` (каталог,
чужой файл без прав) навсегда ронял запись; mode=None, сбрасывающий права,
открывал бы чужим файл, который человек закрыл (или закрывал нужный демону).
"""
from __future__ import annotations

import os
import stat
import types

import pytest

from awgbot.util import fsatomic

pytestmark = pytest.mark.unit


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _block_fixed_tmp(path) -> None:
    """Общее имя временного файла занято (остаток прерванной записи — каталог):
    запись с фиксированным `<файл>.tmp` на нём падает."""
    os.makedirs(f"{path}.tmp")


# ── сам помощник ────────────────────────────────────────────────────────────

def test_mode_none_keeps_the_mode_of_the_existing_file_and_new_files_get_0644(tmp_path):
    p = tmp_path / "conf"
    fsatomic.write_atomic(p, "a")
    assert _mode(p) == fsatomic.NEW_FILE_MODE == 0o644
    os.chmod(p, 0o640)
    fsatomic.write_atomic(p, b"b")
    assert _mode(p) == 0o640 and p.read_bytes() == b"b"
    fsatomic.write_atomic(p, "c", 0o600)
    assert _mode(p) == 0o600


def test_the_temporary_file_is_private_while_it_is_written(tmp_path):
    seen = []

    def fill(f):
        seen.append(_mode(f.fileno()))
        f.write(b"x")
    fsatomic.write_stream(tmp_path / "pub", fill, 0o644)
    assert seen == [0o600] and _mode(tmp_path / "pub") == 0o644


def test_two_writers_of_one_file_do_not_share_a_temporary(tmp_path):
    """Второй писатель стартует посреди записи первого (поток такта и ответ на
    слово пары): у каждого свой временный, оба доходят до подмены, итог — целая
    запись последнего, без исключений и остатков."""
    p = tmp_path / "state"

    def outer(f):
        f.write(b"A" * 1000)
        fsatomic.write_atomic(p, b"B" * 10)
        f.write(b"A" * 1000)
    fsatomic.write_stream(p, outer)
    assert p.read_bytes() == b"A" * 2000
    assert os.listdir(tmp_path) == ["state"]


def test_a_failing_fill_keeps_the_old_file_and_returns_nothing_half_written(tmp_path):
    p = tmp_path / "db"
    fsatomic.write_atomic(p, b"old", 0o600)

    def fill(f):
        f.write(b"new-part")
        raise OSError(28, "No space left on device")
    with pytest.raises(OSError):
        fsatomic.write_stream(p, fill, 0o600)
    assert p.read_bytes() == b"old" and os.listdir(tmp_path) == ["db"]
    with pytest.raises(TypeError):
        fsatomic.write_atomic(p, 12345)                   # не bytes и не str — не 12345 нулевых байт
    assert p.read_bytes() == b"old" and os.listdir(tmp_path) == ["db"]


def test_write_stream_returns_what_fill_returned(tmp_path):
    assert fsatomic.write_stream(tmp_path / "x", lambda f: f.write(b"abc") and "готово") == "готово"


def test_fsync_false_syncs_nothing_and_fsync_true_syncs_file_then_directory(tmp_path, monkeypatch):
    kinds = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (kinds.append(stat.S_ISDIR(os.fstat(fd).st_mode)), real(fd))[1])
    fsatomic.write_atomic(tmp_path / "a", "x", fsync=False)
    assert kinds == []
    fsatomic.write_atomic(tmp_path / "a", "y")
    assert kinds == [False, True]


def test_fsync_dir_of_a_missing_directory_is_silent(tmp_path):
    fsatomic.fsync_dir(tmp_path / "nope")


# ── писатели: занятое общее имя `.tmp` их больше не роняет ──────────────────

def test_the_pair_journal_compaction(tmp_path):
    """Компактизация по снимку переписывает cnt-<сторона>.seg и сегмент событий."""
    from tests.unit.test_rsvjrn_segments import crec, nums, rec
    from awgbot.util.rsvjrn.segments import Store
    store = Store(tmp_path / "jrn")
    store.append_counts("a", [crec(1), crec(2), crec(3)])
    store.append_events([rec(1), rec(2), rec(3)])
    cnt, ev = store._cnt("a"), store.path / "ev-7-2.seg"
    _block_fixed_tmp(cnt)
    _block_fixed_tmp(ev)
    store.drop_counts_upto("a", 1)
    assert store.drop_events_upto(1) == 1
    assert nums(store.counts("a")) == [2, 3] and nums(store.events()) == [2, 3]
    assert _mode(cnt) == 0o600 and _mode(ev) == 0o600


def test_the_guest_storage_and_its_stream(tmp_path):
    from awgbot.infra import rsvzone
    p = tmp_path / "rep.bin"
    _block_fixed_tmp(p)
    rsvzone.write_private(p, b"rep")
    assert rsvzone.write_private_with(tmp_path / "bot.db", lambda f: f.write(b"db") and 2) == 2
    assert p.read_bytes() == b"rep" and _mode(p) == 0o600 and _mode(tmp_path / "bot.db") == 0o600


def test_the_base_snapshot_on_the_reserve(tmp_path, monkeypatch):
    from awgbot.core import config
    from awgbot.domain.services.reserve.package import ReservePackageMixin
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    out = tmp_path / "reserve" / "out"
    out.mkdir(parents=True)
    _block_fixed_tmp(out / "base-3.db")
    ReservePackageMixin._pkg_store_base(None, 3, b"sqlite")
    assert (out / "base-3.db").read_bytes() == b"sqlite" and _mode(out / "base-3.db") == 0o600


def test_the_dnsmasq_lists_conf_keeps_its_mode(tmp_path, monkeypatch):
    from awgbot.infra.routing import feeds
    monkeypatch.setattr(feeds.subprocess, "run",
                        lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
    p = tmp_path / "awg-routing.conf"
    p.write_text("old\n", encoding="utf-8")
    os.chmod(p, 0o640)
    _block_fixed_tmp(p)
    assert feeds.write_dnsmasq_conf("ipset=/x/awg\n", str(p)) is True
    assert p.read_text(encoding="utf-8") == "ipset=/x/awg\n" and _mode(p) == 0o640


def test_the_alive_mark_for_the_update_guard(tmp_path, monkeypatch):
    from awgbot.domain.selfupdate import marks
    monkeypatch.setattr(marks, "_alive_done", False)
    monkeypatch.setattr(marks, "_commit_now", lambda: None)
    open(marks.data_path(marks.UPDATE_GUARD_FILE, str(tmp_path)), "w").close()
    ok = marks.data_path(marks.UPDATE_OK_FILE, str(tmp_path))
    _block_fixed_tmp(ok)
    assert marks.mark_alive("тест", str(tmp_path)) is True
    assert os.path.isfile(ok)
