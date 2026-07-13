"""Integration: создание бэкапа (make_backup) и выбор параметров шифрования."""
import pytest

from awgbot.core import config
from awgbot.domain.services import Services
from awgbot.util import secrets_util

pytestmark = pytest.mark.integration


def _patch_sources(monkeypatch, tmp_path, *, db_bytes=b"SQLITE-DATA", with_db=True):
    dbp = tmp_path / "src_bot.db"          # отдельно от файла db-фикстуры (tmp_path/bot.db)
    if with_db:
        dbp.write_bytes(db_bytes)
    monkeypatch.setattr(config, "DB_PATH", dbp)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")

    def fake_read(path):
        if path == config.CONF_PATH:
            return "[Interface]\nPrivateKey = X\n"
        if path == config.CLIENTS_TABLE_PATH:
            return '[{"clientId":"pub"}]'
        return ""
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "read_file", fake_read, raising=False)


def test_make_backup_plaintext(services, fake_awg, monkeypatch, tmp_path):
    _patch_sources(monkeypatch, tmp_path, db_bytes=b"DBDATA")
    monkeypatch.setattr(config, "BACKUP_ENCRYPTION_ENABLED", False)
    paths = services.make_backup()
    assert len(paths) == 3
    assert not any(p.endswith(".enc") for p in paths)
    db_file = next(p for p in paths if p.endswith(".db"))
    with open(db_file, "rb") as f:
        assert f.read() == b"DBDATA"


def test_make_backup_encrypted_roundtrips(services, fake_awg, monkeypatch, tmp_path):
    _patch_sources(monkeypatch, tmp_path, db_bytes=b"SECRET-DB")
    monkeypatch.setattr(config, "BACKUP_ENCRYPTION_ENABLED", True)
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "correct horse")
    monkeypatch.setattr(config, "BACKUP_KEY", "")
    paths = services.make_backup()
    assert paths and all(p.endswith(".enc") for p in paths)
    db_enc = next(p for p in paths if ".db.enc" in p)
    with open(db_enc, "rb") as f:
        blob = f.read()
    assert secrets_util.inspect_mode(blob) == "passphrase"
    assert secrets_util.decrypt(blob, passphrase="correct horse") == b"SECRET-DB"


def test_make_backup_skips_missing_db(services, fake_awg, monkeypatch, tmp_path):
    _patch_sources(monkeypatch, tmp_path, with_db=False)
    monkeypatch.setattr(config, "BACKUP_ENCRYPTION_ENABLED", False)
    paths = services.make_backup()
    assert len(paths) == 2
    assert not any(p.endswith(".db") for p in paths)


def test_backup_enc_kwargs_prefers_passphrase(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "phrase")
    monkeypatch.setattr(config, "BACKUP_KEY", secrets_util.b64e(bytes(32)))
    assert Services._backup_enc_kwargs() == {"passphrase": "phrase"}


def test_backup_enc_kwargs_key_when_no_passphrase(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "")
    monkeypatch.setattr(config, "BACKUP_KEY", secrets_util.b64e(b"k" * 32))
    assert Services._backup_enc_kwargs() == {"key": b"k" * 32}
