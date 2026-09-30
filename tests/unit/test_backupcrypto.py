"""Секрет шифрования бэкапов: в БД, фраза важнее ключа, переезд из env."""
from __future__ import annotations

import base64

import pytest
from nacl import utils as nacl_utils

import awgbot.core.config as cfg
from awgbot.domain.backupcrypto import MIN_PASSPHRASE_LEN


def test_passphrase_lives_in_db(services):
    assert not services.backup_encryption_enabled() and services.backup_encryption_mode() == ""
    with pytest.raises(ValueError):
        services.backup_set_passphrase("short")
    services.backup_set_passphrase("a" * MIN_PASSPHRASE_LEN)
    assert services.backup_encryption_mode() == "passphrase"
    assert services.backup_enc_kwargs() == {"passphrase": "a" * MIN_PASSPHRASE_LEN}


def test_random_key_of_the_old_scheme_yields_to_passphrase(services):
    """Случайный ключ, перенесённый в БД до v2.10.0, продолжает действовать,
    пока не задана фраза."""
    key = nacl_utils.random(32)
    services.db.set_state(services._BK_KEY_KEY, base64.b64encode(key).decode())
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
    # на шлюзе открытая копия не принимается вовсе: restore раскладывает её от root
    gw_info = services.inspect_backup(_archive_with_meta("gw"), "b.tgz")
    assert not gw_info["ok"] and "шифрованные" in gw_info["error"], gw_info


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
    assert warn.startswith("Все соединения оборвутся на несколько секунд")
    assert texts.restore_offer("2026-09-09T10:00:00+03:00", warn).endswith(warn)
    assert warn not in texts.restore_offer("2026-09-09T10:00:00+03:00")
    assert texts.awg_restart_warning_body(True).startswith("Линк опустится и поднимется")


def test_db_snapshot_carries_transactions_still_in_the_wal(tmp_path):
    """Бот пишет в WAL; побайтовое чтение bot.db отдавало базу без последних
    транзакций — устройство без ключа после восстановления. backup API снимает
    согласованную копию и при открытом писателе."""
    import sqlite3
    from awgbot.domain.backupcrypto import BackupCryptoMixin
    path = tmp_path / "bot.db"
    w = sqlite3.connect(path)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("CREATE TABLE t (v TEXT)")
    w.commit()
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    w.execute("INSERT INTO t VALUES ('in-wal')")
    w.commit()                                   # лежит в bot.db-wal, писатель открыт
    raw = BackupCryptoMixin.db_snapshot_bytes(path)
    assert raw and raw[:16] == b"SQLite format 3\x00"
    assert b"in-wal" not in path.read_bytes(), "тест не воспроизводит WAL — строка уже в файле"
    snap = tmp_path / "snap.db"
    snap.write_bytes(raw)
    assert sqlite3.connect(snap).execute("SELECT v FROM t").fetchall() == [("in-wal",)]
    w.close()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".snap-")], "временный файл снимка остался"
    assert BackupCryptoMixin.db_snapshot_bytes(tmp_path / "nope.db") is None


def test_backup_extra_depends_on_the_role(services, make_active_client, monkeypatch, tmp_path):
    """Один список «что сверх БД» на копию из чата, awg-bot backup и снимок
    перед восстановлением: у сервера — конфиг интерфейса и линки слотов, у
    шлюза — все конфиги интерфейсов, firewall.env и личные списки."""
    from awgbot.infra import awg, gwguard
    monkeypatch.setattr(awg, "read_file", lambda p: "[Interface]\nPrivateKey = DUMMY\n")
    awgdir = tmp_path / "awg"; awgdir.mkdir()
    (awgdir / "awglink.conf").write_text("link", encoding="utf-8")
    monkeypatch.setattr(cfg, "AWG_DIR", str(awgdir))
    dev = services.add_device(make_active_client(device_limit=3).id, "pi")
    services.db.gateway_add(dev.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    names = [n for n, _ in services.backup_extra()]
    assert f"awg/{cfg.AWG_INTERFACE}.conf" in names and "awg/awglink.conf" in names, names
    assert not any(n.startswith("awg-gw/") for n in names)

    monkeypatch.setattr(cfg, "ROLE", "gateway")
    gwdir = tmp_path / "gw"; gwdir.mkdir()
    for n in ("awg0.conf", "awglink.conf"):
        (gwdir / n).write_text(n, encoding="utf-8")
    monkeypatch.setattr(cfg, "GW_CONF_DIR", str(gwdir))
    fw = tmp_path / "firewall.env"; fw.write_text("ADMIN_IPS_EXTRA=\n", encoding="utf-8")
    monkeypatch.setattr(gwguard, "FW_ENV", str(fw))
    names = [n for n, _ in services.backup_extra()]
    assert names[:2] == ["awg/awg0.conf", "awg/awglink.conf"] and "awg-gw/firewall.env" in names, names
