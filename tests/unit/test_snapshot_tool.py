"""
tools/snapshot.py — `awg-bot backup`: тот же состав и тот же сборщик, что у
копии из чата, только шифрованный ключевой фразой из базы. Без фразы —
отказ словами и файла нет; на шлюзе бэкапов нет вовсе; запасного открытого
снимка оболочкой нет.
"""
from __future__ import annotations

import io
import json
import stat
import tarfile
from pathlib import Path

import pytest

import awgbot.core.config as cfg
from awgbot.infra.db import Database
from awgbot.util import secrets_util

pytestmark = pytest.mark.unit

PHRASE = "correct horse battery"


def _host(tmp_path, monkeypatch, *, phrase: str = PHRASE):
    db_path = tmp_path / "bot.db"
    d = Database(str(db_path)); d.init_schema()
    if phrase:
        d.set_state("backup_passphrase", phrase)
    d.close()
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "app.yaml").write_text("role: client\n", encoding="utf-8")
    env = tmp_path / "env"; env.write_text("BOT_TOKEN=DUMMY\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "ROLE", "client")
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    monkeypatch.setattr(cfg, "CONF_DIR", conf)
    monkeypatch.setattr(cfg, "ENV_PATH", env)
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "read_file", lambda p: "[Interface]\nPrivateKey = DUMMY\n")


def test_backup_is_encrypted_with_the_current_phrase(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch)
    from tools import snapshot
    out = tmp_path / "awg-bot-backup-main-x.tgz.enc"
    assert snapshot.main(["--out", str(out)]) == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600, "бэкап с секретами читаем не только root"
    assert not Path(f"{out}.part").exists()
    blob = out.read_bytes()
    assert blob.startswith(secrets_util.MAGIC), "открытый бэкап"
    with tarfile.open(fileobj=io.BytesIO(secrets_util.decrypt(blob, passphrase=PHRASE)), mode="r:gz") as tar:
        names = tar.getnames()
        meta = json.loads(tar.extractfile("state/backup-meta.json").read())
    assert {"state/backup-meta.json", "state/bot.db", "state/conf/app.yaml", "state/env",
            f"awg/{cfg.AWG_INTERFACE}.conf"} <= set(names), names
    assert meta["role"] == "main" and meta["schema"] == 2 and meta["created_at"]


def test_without_a_phrase_there_is_no_backup(tmp_path, monkeypatch, capsys):
    _host(tmp_path, monkeypatch, phrase="")
    from tools import snapshot
    out = tmp_path / "awg-bot-backup-main-x.tgz.enc"
    assert snapshot.main(["--out", str(out)]) == 1
    assert not out.exists() and not Path(f"{out}.part").exists()
    assert capsys.readouterr().err.strip() == (
        "без ключевой фразы бэкап не сделать: открытых бэкапов нет. Задай фразу в боте сервера AWG: "
        "«💾 Бэкапы» → «🔐 Шифрование»")


def test_the_gateway_has_no_backups(tmp_path, monkeypatch, capsys):
    _host(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg, "ROLE", "gateway")
    from tools import snapshot
    out = tmp_path / "x.tgz.enc"
    assert snapshot.main(["--out", str(out)]) == 1 and not out.exists()
    assert capsys.readouterr().err.startswith("на шлюзе бэкапов нет: настройки шлюза — в бэкапе сервера AWG")


def test_snapshot_reports_failure_instead_of_a_half_file(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch)
    from awgbot.domain.backup.archive import BackupArchiveMixin
    monkeypatch.setattr(BackupArchiveMixin, "build_backup_archive",
                        lambda self, extra=None: (_ for _ in ()).throw(RuntimeError("boom")))
    from tools import snapshot
    out = tmp_path / "snap.tgz.enc"
    assert snapshot.main(["--out", str(out)]) == 1
    assert not out.exists() and not Path(f"{out}.part").exists()


def test_the_shell_has_no_open_fallback_snapshot():
    """Запасной снимок оболочкой при неработающем Python снят: шифровать без
    кода установки нечем, а открытых бэкапов нет."""
    script = (Path(__file__).resolve().parents[2] / "awg-bot.sh").read_text(encoding="utf-8")
    body = script.split("cmd_backup() {", 1)[1].split("\n}\n", 1)[0]
    assert "tools.snapshot --out" in body and ".tgz.enc" in body
    assert "snapshot_state" not in script and "wal_checkpoint" not in script and "tar czf" not in body
    assert 'is_gateway_role && die "$GW_NO_BACKUPS"' in body
