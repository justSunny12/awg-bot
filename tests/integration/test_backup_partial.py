"""
Копия без базы (снимок БД не снялся) — частичная, а не обычная.

Стережём:
- метка partial в описи и «-partial-» в имени файла: раньше архив без
  state/bot.db уходил как обычная копия;
- ротация не вытесняет последнюю полную копию ради частичных: пять подряд
  неснявшихся снимков иначе оставляли в каталоге одни копии без базы;
- подпись копии предупреждает админа (в чате и в письме — одна подпись);
- файл копии — 0600 с первого байта (атомарная запись), а не write_bytes и
  chmod после.
"""
from __future__ import annotations

import io
import json
import os
import stat
import tarfile

import pytest

import awgbot.core.config as cfg
from awgbot.domain.notices import backup as nb

pytestmark = pytest.mark.integration

PHRASE = "DUMMY-phrase-for-tests"


@pytest.fixture()
def phrase(services):
    services.backup_set_passphrase(PHRASE)


def _open(path: str) -> tarfile.TarFile:
    from awgbot.util import secrets_util
    with open(path, "rb") as f:
        raw = secrets_util.decrypt(f.read(), passphrase=PHRASE)
    return tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")


def test_a_failed_db_snapshot_makes_a_marked_partial_copy(services, phrase, monkeypatch):
    def boom(_path):
        raise OSError("disk I/O error")
    monkeypatch.setattr(services, "db_snapshot_bytes", boom)
    [path] = services.make_backup()
    assert "-partial-" in os.path.basename(path)
    with _open(path) as tar:
        names = tar.getnames()
        meta = json.loads(tar.extractfile("state/backup-meta.json").read())
    assert "state/bot.db" not in names and meta.get("partial") is True
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert nb.PARTIAL_LINE in services.backup_caption()


def test_a_full_copy_is_not_marked(services, phrase, monkeypatch):
    monkeypatch.setattr(cfg, "DB_PATH", services.db.path)
    [path] = services.make_backup()
    assert "-partial-" not in os.path.basename(path)
    with _open(path) as tar:
        assert "state/bot.db" in tar.getnames()
        assert "partial" not in json.loads(tar.extractfile("state/backup-meta.json").read())
    assert nb.PARTIAL_LINE not in services.backup_caption()


def test_rotation_keeps_the_last_full_copy_behind_partials(services):
    d = cfg.BACKUP_DIR
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("awg-bot-backup-*"):
        f.unlink()
    full = d / "awg-bot-backup-main-20261001_000000.tgz.enc"
    full.write_bytes(b"x")
    os.utime(full, (1000, 1000))
    partials = []
    for i in range(services.KEEP_BACKUPS + 2):
        p = d / f"awg-bot-backup-main-partial-2026100{i + 2}_000000.tgz.enc"
        p.write_bytes(b"x")
        os.utime(p, (2000 + i, 2000 + i))
        partials.append(p)
    services._rotate_backups()
    left = {p.name for p in d.glob("awg-bot-backup-*")}
    assert full.name in left, "последняя полная копия вытеснена частичными"
    assert left == {full.name} | {p.name for p in partials[-services.KEEP_BACKUPS:]}


def test_partial_line_reads_as_a_caption_line():
    text = nb.backup_caption("01.10.26 10:00 (UTC+5)", [], [], partial=True)
    head, line = text.split("\n")
    assert line == nb.PARTIAL_LINE and not line.endswith(".")
