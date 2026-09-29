"""
tools/snapshot.py — снимок состояния для `awg-bot backup` и снимка перед
восстановлением: тот же состав и тот же сборщик, что у копии из чата.
"""
from __future__ import annotations

import json
import stat
import tarfile

import pytest

import awgbot.core.config as cfg
from awgbot.infra.db import Database

pytestmark = pytest.mark.unit


def test_snapshot_writes_the_same_layout_as_the_chat_copy(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"
    d = Database(str(db_path)); d.init_schema(); d.close()
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "app.yaml").write_text("role: client\n", encoding="utf-8")
    env = tmp_path / "env"; env.write_text("BOT_TOKEN=DUMMY\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "ROLE", "client")
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "CONF_DIR", conf)
    monkeypatch.setattr(cfg, "ENV_PATH", env)
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "read_file", lambda p: "[Interface]\nPrivateKey = DUMMY\n")
    from tools import snapshot
    out = tmp_path / "snap.tgz"
    assert snapshot.main(["--out", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600, "снимок с секретами читаем не только root"
    assert not (tmp_path / "snap.tgz.part").exists()
    with tarfile.open(out, mode="r:gz") as tar:
        names = tar.getnames()
        meta = json.loads(tar.extractfile("state/backup-meta.json").read())
    assert {"state/backup-meta.json", "state/bot.db", "state/conf/app.yaml", "state/env",
            f"awg/{cfg.AWG_INTERFACE}.conf"} <= set(names), names
    assert meta["role"] == "main" and meta["created_at"]


def test_snapshot_reports_failure_instead_of_a_half_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "ROLE", "client")
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "nope.db")
    from awgbot.domain.backupcrypto import BackupCryptoMixin
    monkeypatch.setattr(BackupCryptoMixin, "build_backup_archive",
                        lambda self, extra=None: (_ for _ in ()).throw(RuntimeError("boom")))
    from tools import snapshot
    out = tmp_path / "snap.tgz"
    assert snapshot.main(["--out", str(out)]) == 1
    assert not out.exists() and not (tmp_path / "snap.tgz.part").exists()
