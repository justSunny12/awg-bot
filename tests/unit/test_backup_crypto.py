"""Секрет шифрования бэкапов сервера AWG: только фраза, в БД; бэкапы — только у сервера."""
from __future__ import annotations

import base64

import pytest
from nacl import utils as nacl_utils

import awgbot.core.config as cfg
from awgbot.domain.backup.crypto import MIN_PASSPHRASE_LEN


def test_passphrase_lives_in_db(services):
    assert not services.backup_encryption_enabled() and services.backup_encryption_mode() == ""
    with pytest.raises(ValueError):
        services.backup_set_passphrase("short")
    services.backup_set_passphrase("a" * MIN_PASSPHRASE_LEN)
    assert services.backup_encryption_mode() == "passphrase"
    assert services.backup_enc_kwargs() == {"passphrase": "a" * MIN_PASSPHRASE_LEN}


def test_the_random_key_of_the_old_scheme_is_no_encryption(services, monkeypatch):
    """Фраза обязательна: случайный ключ, перенесённый в БД до v2.10.0,
    шифрованием не считается ни у какой роли — раздел и копии как без фразы."""
    key = nacl_utils.random(32)
    services.db.set_state("backup_key", base64.b64encode(key).decode())
    for role in ("client", "gateway"):
        monkeypatch.setattr(cfg, "ROLE", role)
        assert services.backup_encryption_mode() == "" and services.backup_enc_kwargs() is None
        assert not services.backup_encryption_enabled()
    services.backup_set_passphrase("correct horse battery")
    assert services.backup_encryption_mode() == "passphrase"


def test_make_backup_without_secret_makes_no_file(services, monkeypatch, tmp_path):
    """Открытых копий нет: без секрета — отказ, файла нет; с ним — .tgz.enc."""
    from awgbot.domain.backup.crypto import BackupKeyMissing
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "bot.db")
    (tmp_path / "bot.db").write_bytes(b"db")
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "read_file", lambda p: (_ for _ in ()).throw(awg.AwgError("no conf")))
    with pytest.raises(BackupKeyMissing):
        services.make_backup()
    assert not list(tmp_path.glob("awg-bot-backup-*")), "без фразы файл копии появился"
    services.backup_set_passphrase("correct horse battery")
    paths = services.make_backup()
    assert paths and paths[0].endswith(".tgz.enc")


PHRASE = "correct horse battery"


def _archive(meta: dict | None = None, *, env: str = "", db: bytes | None = None) -> bytes:
    import io, json, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, raw):
            ti = tarfile.TarInfo(name); ti.size = len(raw); tar.addfile(ti, io.BytesIO(raw))
        if meta is not None:
            add("state/backup-meta.json", json.dumps(meta).encode())
        if env:
            add("state/env", env.encode())
        if db is not None:
            add("state/bot.db", db)
    return buf.getvalue()


def _meta(**kw) -> dict:
    from awgbot.infra import awglock
    m = {"schema": 2, "role": "main", "created_at": "2026-10-09T10:00:00+03:00", "version": "3.4.0",
         "awg_gen": awglock.generation(), "db_line": "", "gateways": []}
    m.update(kw)
    return m


@pytest.fixture()
def v340(monkeypatch):
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.4.0")
    monkeypatch.setattr(cfg, "ROLE", "client")


def test_backup_meta_is_schema_two_inside_the_archive(services, monkeypatch, tmp_path, v340):
    import io, json, tarfile
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "nope.db")
    monkeypatch.setattr(cfg, "ENV_PATH", None)
    monkeypatch.setattr(cfg, "CONF_DIR", tmp_path / "noconf")
    raw = services.build_backup_archive()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        meta = json.loads(tar.extractfile("state/backup-meta.json").read())
    assert meta["schema"] == 2 and meta["role"] == "main" and meta["version"] == "3.4.0" and meta["created_at"]
    assert meta["db_line"] == services.db.get_state("db_line") and len(meta["db_line"]) == 16
    assert "awg_gen" in meta and "digest" in meta and meta["gateways"] == []


def test_inspect_refuses_with_words(services, monkeypatch, tmp_path, v340):
    """Отказы осмотра: открытый файл, схема 1, версия ниже 3.4.0, новее кода,
    другое поколение, другой админ, копия шлюза прежних версий, фраза."""
    from awgbot.util import secrets_util
    db = services.db_snapshot_bytes(services.db.path)

    def enc(meta, env=f"ADMIN_ID={cfg.ADMIN_ID}\n", phrase=PHRASE):
        return secrets_util.encrypt(_archive(meta, env=env, db=db), passphrase=phrase)

    def err(blob):
        r = services.inspect_backup(blob, "b.tgz.enc")
        assert not r["ok"], r
        return r["error"]

    assert "не зашифрован" in err(_archive(_meta(), db=db))
    assert "не задана" in err(enc(_meta()))
    services.backup_set_passphrase(PHRASE)
    assert "не та" in err(enc(_meta(), phrase="wrong-phrase!"))
    assert err(enc({"role": "main", "created_at": "2026-09-09T10:00:00+03:00", "version": "3.3.1"})) == \
        "бэкап снят версией 3.3.1 — бэкапы версий до 3.4.0 не применяются"
    assert err(enc(_meta(version="3.3.1"))) == "бэкап снят версией 3.3.1 — бэкапы версий до 3.4.0 не применяются"
    assert err(enc(_meta(version="3.5.0"))) == \
        "бэкап снят версии 3.5.0, а здесь 3.4.0 — обнови бота до 3.5.0, затем восстанови"
    from awgbot.infra import awglock
    monkeypatch.setattr(awglock, "generation", lambda: 2)
    assert "другом поколении AmneziaWG" in err(enc(_meta(awg_gen=1)))
    monkeypatch.setattr(awglock, "generation", lambda: 1)
    assert err(enc(_meta(awg_gen=1), env="ADMIN_ID=4242\n")) == \
        "бэкап другого админа — из чата его не восстановить; на сервере: sudo awg-bot restore <файл>"
    assert "бэкап шлюза прежних версий" in err(enc({"role": "gw", "created_at": "2026-09-09T10:00:00+03:00"}))
    assert not services.inspect_backup(b"garbage", "b.tgz")["ok"]
    ok = services.inspect_backup(enc(_meta(awg_gen=1, version="3.4.0.2")), "x.bin")
    assert ok["ok"] and ok["created_at"].startswith("2026-10-09") and ok["plan"]["line"] in ("same", "clean")


def test_a_hotfix_of_the_same_version_is_not_newer(v340):
    from awgbot.core import compat
    assert not compat.newer_than("3.4.0.3", "3.4.0") and compat.newer_than("3.4.1", "3.4.0.9")
    assert compat.below_floor("3.3.1") and not compat.below_floor("3.4.0") and compat.below_floor("")


def test_the_gateway_has_no_backups(services, tmp_path):
    """Своих копий у шлюза нет: у агента ни сборки, ни осмотра, ни
    восстановления, ни фразы; копия шлюза прежних версий у сервера — отказ."""
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    from awgbot.util import secrets_util
    db = Database(str(tmp_path / "gw.db")); db.init_schema()
    try:
        gw = GatewayServices(db)
        for name in ("make_backup", "inspect_backup", "prepare_restore", "backup_enc_kwargs",
                     "backup_set_passphrase", "write_backup_archive", "email_send_backup", "backup_channel"):
            assert not hasattr(gw, name), name
    finally:
        db.close()
    services.backup_set_passphrase(PHRASE)
    r = services.inspect_backup(secrets_util.encrypt(_archive(_meta(role="gw")), passphrase=PHRASE), "b.tgz.enc")
    assert not r["ok"] and "бэкап шлюза прежних версий" in r["error"], r


def test_db_snapshot_carries_transactions_still_in_the_wal(tmp_path):
    """Бот пишет в WAL; побайтовое чтение bot.db отдавало базу без последних
    транзакций — устройство без ключа после восстановления. backup API снимает
    согласованную копию и при открытом писателе."""
    import sqlite3
    from awgbot.domain.backup.archive import BackupArchiveMixin
    path = tmp_path / "bot.db"
    w = sqlite3.connect(path)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("CREATE TABLE t (v TEXT)")
    w.commit()
    w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    w.execute("INSERT INTO t VALUES ('in-wal')")
    w.commit()                                   # лежит в bot.db-wal, писатель открыт
    raw = BackupArchiveMixin.db_snapshot_bytes(path)
    assert raw and raw[:16] == b"SQLite format 3\x00"
    assert b"in-wal" not in path.read_bytes(), "тест не воспроизводит WAL — строка уже в файле"
    snap = tmp_path / "snap.db"
    snap.write_bytes(raw)
    assert sqlite3.connect(snap).execute("SELECT v FROM t").fetchall() == [("in-wal",)]
    w.close()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".snap-")], "временный файл снимка остался"
    assert BackupArchiveMixin.db_snapshot_bytes(tmp_path / "nope.db") is None


def test_backup_extra_is_the_servers(services, make_active_client, monkeypatch, tmp_path):
    """Один список «что сверх БД» на копию из чата, по почте и awg-bot backup:
    конфиг интерфейса и линки слотов; шлюзовой ветки состава больше нет."""
    from awgbot.infra import awg
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
    assert [n for n, _ in services.backup_extra()] == names and services.backup_role() == "main"


def test_drop_after_restore_goes_through_the_bots_single_entry(monkeypatch, tmp_path):
    """--apply-blocks — тот же awg.block_ip и тот же вход seam._exec, что у
    бота: на хосте — iptables прямо, в docker-режиме — docker exec в контейнер."""
    import json
    from awgbot.infra import awg
    from tools import restore_merge
    report = tmp_path / "restore-merge.json"
    report.write_text(json.dumps({"blocked": ["10.8.1.5"]}), encoding="utf-8")
    runs = []

    class P:
        def __init__(self, args):
            self.returncode = 1 if "-C" in args else 0     # «правила ещё нет»
            self.stdout = self.stderr = b""
    monkeypatch.setattr(awg, "_run", lambda args, **kw: runs.append(list(args)) or P(args))
    monkeypatch.setattr(awg, "in_container", lambda: True)
    assert restore_merge.main(["--apply-blocks", str(report)]) == 0
    assert ["docker", "exec", cfg.CONTAINER, "iptables", "-I", "FORWARD", "1", "-s", "10.8.1.5/32", "-j", "DROP"] \
        in runs, runs
    runs.clear()
    monkeypatch.setattr(awg, "in_container", lambda: False)
    assert restore_merge.main(["--apply-blocks", str(report)]) == 0
    assert ["iptables", "-I", "FORWARD", "1", "-s", "10.8.1.5/32", "-j", "DROP"] in runs, runs
