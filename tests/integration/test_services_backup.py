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


def _names(tgz_bytes):
    import io, tarfile
    with tarfile.open(fileobj=io.BytesIO(tgz_bytes), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def test_make_backup_is_one_archive_with_everything(services, fake_awg, monkeypatch, tmp_path):
    _patch_sources(monkeypatch, tmp_path, db_bytes=b"DBDATA")
    confd = tmp_path / "conf"; confd.mkdir()
    (confd / "app.yaml").write_text("a: 1\n"); (confd / "email.yaml").write_text("b: 2\n")
    monkeypatch.setattr(config, "CONF_DIR", confd)
    envf = tmp_path / "env"; envf.write_text("BOT_TOKEN=t\n")
    monkeypatch.setattr(config, "ENV_PATH", envf)
    paths = services.make_backup()
    assert len(paths) == 1 and paths[0].endswith(".tgz")             # без секрета — открытый
    files = _names(open(paths[0], "rb").read())
    assert files["state/bot.db"] == b"DBDATA"
    assert files["state/conf/app.yaml"] == b"a: 1\n" and "state/conf/email.yaml" in files
    assert files["state/env"] == b"BOT_TOKEN=t\n"
    assert files[f"awg/{config.AWG_INTERFACE}.conf"].startswith(b"[Interface]")


def test_make_backup_encrypted_roundtrips(services, fake_awg, monkeypatch, tmp_path):
    _patch_sources(monkeypatch, tmp_path, db_bytes=b"SECRET-DB")
    services.backup_set_passphrase("correct horse")
    paths = services.make_backup()
    assert len(paths) == 1 and paths[0].endswith(".tgz.enc")
    blob = open(paths[0], "rb").read()
    assert secrets_util.inspect_mode(blob) == "passphrase"
    files = _names(secrets_util.decrypt(blob, passphrase="correct horse"))
    assert files["state/bot.db"] == b"SECRET-DB"


def test_make_backup_without_db_still_packs_the_rest(services, fake_awg, monkeypatch, tmp_path):
    _patch_sources(monkeypatch, tmp_path, with_db=False)
    files = _names(open(services.make_backup()[0], "rb").read())
    assert "state/bot.db" not in files and any(n.startswith("awg/") for n in files)


def test_backup_enc_kwargs_prefers_passphrase(services, monkeypatch):
    """Фраза важнее ключа: ключ из env переехал, потом задали фразу — действует фраза."""
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "")
    monkeypatch.setattr(config, "BACKUP_KEY", secrets_util.b64e(bytes(32)))
    assert services.backup_import_env_once() is True
    assert services.backup_enc_kwargs() == {"key": bytes(32)}
    services.backup_set_passphrase("phrase-of-eight")
    assert services.backup_enc_kwargs() == {"passphrase": "phrase-of-eight"}


def test_backup_enc_kwargs_none_without_secret(services, monkeypatch):
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "")
    monkeypatch.setattr(config, "BACKUP_KEY", "")
    assert services.backup_import_env_once() is False
    assert services.backup_enc_kwargs() is None and not services.backup_encryption_enabled()
