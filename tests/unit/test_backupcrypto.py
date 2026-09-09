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


def _archive_with_meta(role, created="2026-09-09T10:00:00+03:00"):
    import io, json, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        raw = json.dumps({"role": role, "created_at": created}).encode()
        ti = tarfile.TarInfo("state/backup-meta.json"); ti.size = len(raw)
        tar.addfile(ti, io.BytesIO(raw))
    return buf.getvalue()


def test_backup_meta_is_inside_the_archive_and_restore_is_role_bound(services, monkeypatch, tmp_path):
    import io, json, tarfile
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "nope.db")
    monkeypatch.setattr(cfg, "ENV_PATH", None)
    monkeypatch.setattr(cfg, "CONF_DIR", tmp_path / "noconf")
    monkeypatch.setattr(cfg, "ROLE", "client")
    raw = services.build_backup_archive()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        meta = json.loads(tar.extractfile("state/backup-meta.json").read())
    assert meta["role"] == "main" and meta["created_at"]
    info = services.inspect_backup(raw, "x.tgz")
    assert info["ok"] and info["role"] == "main" and info["created_at"] == meta["created_at"]
    # копия агента на основном — отказ; и наоборот
    assert not services.inspect_backup(_archive_with_meta("gw"), "b.tgz")["ok"]
    monkeypatch.setattr(cfg, "ROLE", "gateway")
    assert services.inspect_backup(_archive_with_meta("gw"), "b.tgz")["ok"]
    assert "основного бота" in services.inspect_backup(_archive_with_meta("main"), "b.tgz")["error"]


def test_inspect_backup_encrypted_needs_the_right_phrase(services, monkeypatch, tmp_path):
    from awgbot.util import secrets_util
    monkeypatch.setattr(cfg, "ROLE", "client")
    blob = secrets_util.encrypt(_archive_with_meta("main"), passphrase="right-phrase")
    assert "не задана" in services.inspect_backup(blob, "b.tgz.enc")["error"]
    services.backup_set_passphrase("wrong-phrase!")
    assert "не та" in services.inspect_backup(blob, "b.tgz.enc")["error"]
    services.backup_set_passphrase("right-phrase")
    info = services.inspect_backup(blob, "b.tgz.enc")
    assert info["ok"] and info["created_at"].startswith("2026-09-09")
    assert not services.inspect_backup(b"garbage", "b.tgz")["ok"]


def test_restore_reports_only_changed_interfaces(services, monkeypatch, tmp_path):
    import io, json, tarfile
    from awgbot.bot import texts
    monkeypatch.setattr(cfg, "ROLE", "client")
    d = tmp_path / "awg"; d.mkdir(); (d / "awg1.conf").write_bytes(b"[Interface]\nA\n")
    monkeypatch.setattr(cfg, "AWG_DIR", str(d))
    def arc(conf):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, raw in (("state/backup-meta.json", json.dumps({"role": "main", "created_at": "2026-09-09T10:00:00+03:00"}).encode()),
                              ("awg/awg1.conf", conf)):
                ti = tarfile.TarInfo(name); ti.size = len(raw); tar.addfile(ti, io.BytesIO(raw))
        return buf.getvalue()
    assert services.inspect_backup(arc(b"[Interface]\nA\n"), "b.tgz")["ifaces_changed"] == []
    assert services.inspect_backup(arc(b"[Interface]\nB\n"), "b.tgz")["ifaces_changed"] == ["awg1"]
    warn = texts.awg_restart_warning_body(False)
    assert warn.startswith("Сервер AmneziaWG перезапустится")
    assert texts.restore_offer("2026-09-09T10:00:00+03:00", warn).endswith(warn)
    assert warn not in texts.restore_offer("2026-09-09T10:00:00+03:00")
    assert texts.awg_restart_warning_body(True).startswith("Интерфейс линка опустится")
