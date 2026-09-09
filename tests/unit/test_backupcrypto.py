"""Секрет шифрования бэкапов: в БД, фраза важнее ключа, переезд из env."""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.domain.backupcrypto import MIN_PASSPHRASE_LEN


def test_passphrase_in_db_and_env_import(services, monkeypatch):
    assert not services.backup_encryption_enabled() and services.backup_encryption_mode() == ""
    monkeypatch.setattr(cfg, "BACKUP_KEY", "")
    monkeypatch.setattr(cfg, "BACKUP_PASSPHRASE", "from-env-phrase")
    assert services.backup_import_env_once() is True
    assert services.backup_encryption_mode() == "passphrase"
    assert services.backup_enc_kwargs() == {"passphrase": "from-env-phrase"}
    assert services.backup_import_env_once() is False
    with pytest.raises(ValueError):
        services.backup_set_passphrase("short")
    services.backup_set_passphrase("a" * MIN_PASSPHRASE_LEN)
    assert services.backup_enc_kwargs() == {"passphrase": "a" * MIN_PASSPHRASE_LEN}


def test_random_key_from_env_migrates_and_yields_to_passphrase(services, monkeypatch):
    from awgbot.util import secrets_util
    key = secrets_util.gen_random_key()
    monkeypatch.setattr(cfg, "BACKUP_PASSPHRASE", "")
    monkeypatch.setattr(cfg, "BACKUP_KEY", secrets_util.b64e(key))
    assert services.backup_import_env_once() is True
    assert services.backup_encryption_mode() == "key" and services.backup_enc_kwargs() == {"key": key}
    services.backup_set_passphrase("correct horse battery")
    assert services.backup_encryption_mode() == "passphrase"


def test_make_backup_encrypts_only_with_secret(services, monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "bot.db")
    (tmp_path / "bot.db").write_bytes(b"db")
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "read_file", lambda p: (_ for _ in ()).throw(awg.AwgError("no conf")))
    paths = services.make_backup()
    assert paths and not paths[0].endswith(".enc")
    services.backup_set_passphrase("correct horse battery")
    paths = services.make_backup()
    assert paths and paths[0].endswith(".enc")
