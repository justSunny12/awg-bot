import base64
"""Integration: создание бэкапа (make_backup) и выбор параметров шифрования."""
import pytest

from awgbot.core import config
from awgbot.util import secrets_util

pytestmark = pytest.mark.integration


def _patch_sources(monkeypatch, tmp_path, *, db_bytes=b"SQLITE-DATA", with_db=True):
    dbp = tmp_path / "src_bot.db"          # отдельно от файла db-фикстуры (tmp_path/bot.db)
    if with_db:
        # настоящая база с меткой: копия снимается backup API SQLite, не побайтово
        import sqlite3
        c = sqlite3.connect(dbp)
        c.execute("CREATE TABLE marker (v TEXT)")
        c.execute("INSERT INTO marker VALUES (?)", (db_bytes.decode(),))
        c.commit(); c.close()
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
    services.backup_set_passphrase("correct horse")
    paths = services.make_backup()
    assert len(paths) == 1 and paths[0].endswith(".tgz.enc")         # открытых копий нет
    files = _names(secrets_util.decrypt(open(paths[0], "rb").read(), passphrase="correct horse"))
    assert files["state/bot.db"].startswith(b"SQLite format 3") and b"DBDATA" in files["state/bot.db"]
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
    assert b"SECRET-DB" in files["state/bot.db"]


def test_make_backup_without_db_still_packs_the_rest(services, fake_awg, monkeypatch, tmp_path):
    _patch_sources(monkeypatch, tmp_path, with_db=False)
    services.backup_set_passphrase("correct horse")
    files = _names(secrets_util.decrypt(open(services.make_backup()[0], "rb").read(), passphrase="correct horse"))
    assert "state/bot.db" not in files and any(n.startswith("awg/") for n in files)


def test_backup_enc_kwargs_prefers_passphrase(services):
    """Случайный ключ прежней схемы у сервера не действует; задали фразу —
    действует фраза."""
    services.db.set_state("backup_key", base64.b64encode(bytes(32)).decode())
    assert services.backup_enc_kwargs() is None, "у сервера случайный ключ — не шифрование"
    services.backup_set_passphrase("phrase-of-eight")
    assert services.backup_enc_kwargs() == {"passphrase": "phrase-of-eight"}


def test_backup_enc_kwargs_none_without_secret(services):
    assert services.backup_enc_kwargs() is None and not services.backup_encryption_enabled()


# ── метка схемы 2, опись шлюзов, копия по событию ────────────────────────────

PHRASE = "correct horse"


@pytest.fixture()
def slot(services, make_active_client, fake_awg, monkeypatch, tmp_path):
    """Шлюз в слоте 1: конфиг линка ВПС и выданная шлюзовая сторона на диске."""
    from awgbot.domain.backup import meta
    awgd = tmp_path / "awg"; awgd.mkdir()
    root = tmp_path / "root"; root.mkdir()
    (awgd / "awglink.conf").write_text("[Interface]\nPrivateKey = DUMMY\n\n[Peer]\nPublicKey = GWPUB\n",
                                       encoding="utf-8")
    (root / "gw-awglink.conf").write_text("[Interface]\nPrivateKey = DUMMY\n\n[Peer]\nPublicKey = VPSPUB\n",
                                          encoding="utf-8")
    monkeypatch.setattr(config, "AWG_DIR", str(awgd))
    monkeypatch.setattr(meta, "ROOT_DIR", str(root))
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    dev = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(dev.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    return dev


def _meta_of(path):
    import io, json, tarfile
    raw = secrets_util.decrypt(open(path, "rb").read(), passphrase=PHRASE)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        return json.loads(tar.extractfile("state/backup-meta.json").read())


def test_the_label_carries_schema_line_digest_and_the_gateway_inventory(services, slot, monkeypatch, tmp_path):
    _patch_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.0")
    services.db.set_state("update_installed_digest", "3.4.0 abc123")
    services.db.set_state("gwlink_seen_1", "2026-10-12T09:00:00+03:00")
    services.db.set_state("gwkeep_1", '{"v": 1, "at": "2026-10-12T08:40:00+03:00"}')
    services.db.set_state("gwlink_snap_1", '{"agent_version": "3.4.0"}')
    services.backup_set_passphrase(PHRASE)
    m = _meta_of(services.make_backup()[0])
    assert m["schema"] == 2 and m["version"] == "3.4.0" and m["digest"] == "sha256:abc123"
    assert m["db_line"] == services.db.get_state("db_line")
    g = m["gateways"][0]
    assert (g["slot"], g["link_if"], g["name"], g["device_pub"]) == (1, "awglink", "NASPi",
                                                                     services.db.get_device(slot.device_id).public_key)
    assert (g["link_gw_pub"], g["link_vps_pub"], g["agent"]) == ("GWPUB", "VPSPUB", "3.4.0")
    assert (g["seen"], g["keep_at"]) == ("2026-10-12T09:00:00+03:00", "2026-10-12T08:40:00+03:00")
    services.db.set_state("update_installed_digest", "3.3.9 abc123")       # digest другой версии — не наш
    assert _meta_of(services.make_backup()[0])["digest"] == ""


def test_the_caption_names_gateways_and_the_offline_one(services, slot, monkeypatch):
    services.db.gateway_update(1, label="дача")
    services.db.set_state("gwlink_seen_1", "2026-10-12T09:00:00+03:00")
    services.db.set_state("gwkeep_1", '{"v": 1, "at": "2026-10-12T08:40:00+03:00"}')
    lines = services.backup_caption().split("\n")
    assert lines[0].startswith("💾 Бэкап сервера AWG от ") and lines[0].endswith(")")
    assert lines[1:] == ["🛰 Шлюзы: «NASPi» (дача)",
                         "🟡 «NASPi» (дача) не на связи с 12.10.26 — его настройки в бэкапе на 12.10.26"]
    assert "после" not in lines[0]


def test_without_a_phrase_there_is_no_backup_anywhere(services, slot, fake_bot, monkeypatch, tmp_path):
    from awgbot.domain.backup.crypto import BackupKeyMissing
    from awgbot.runtime import scheduler as sch
    _patch_sources(monkeypatch, tmp_path)
    with pytest.raises(BackupKeyMissing):
        services.make_backup()
    services.backup_event_mark("rekey", 1)
    assert not services.db.get_state("backup_event_due"), "событие без фразы отмечено"
    sent = []
    from awgbot.bot import notifier
    async def capture(bot, notes):
        sent.extend(notes)
    monkeypatch.setattr(notifier, "send_notifications", capture)
    services.db.set_state("last_backup", "2000-01")
    import asyncio
    asyncio.run(sch.monthly_backup(services, fake_bot, "backup"))
    assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").glob("*"))
    assert [n.text for n in sent] == [
        "⚠️ Плановый бэкап сервера AWG не сделан: ключевая фраза шифрования не задана, а в бэкапе ключи линков "
        "шлюзов. Задай фразу в «💾 Бэкапы» → «🔐 Шифрование»"]
    assert services.db.get_state("last_backup") == "2000-01"


def test_a_backup_waits_for_the_end_of_a_link_rekey(services, slot, fake_bot, monkeypatch, tmp_path):
    from awgbot.runtime import scheduler as sch
    _patch_sources(monkeypatch, tmp_path)
    services.backup_set_passphrase(PHRASE)
    services.db.set_state("last_backup", "2000-01")
    monkeypatch.setattr(services, "gateway_rekey_active", lambda sid: True)
    made, busy = [], []
    monkeypatch.setattr(services, "make_backup", lambda: made.append(1) or [])
    import asyncio
    asyncio.run(sch.monthly_backup(services, fake_bot, "backup", on_busy=lambda: busy.append(1)))
    assert made == [] and busy == [1] and services.db.get_state("last_backup") == "2000-01"
    services.backup_event_mark("rekey", 1)
    services.db.set_state("backup_event_due", services.db.get_state("backup_event_due").replace(
        '"at": "', '"at": "1'))                                         # отметка «давно» — но смена идёт
    assert services.backup_event_take() is None


def test_an_event_series_makes_one_backup_and_the_mark_goes_only_after_delivery(services, slot, fake_bot,
                                                                                  monkeypatch, tmp_path):
    import asyncio, json
    from awgbot.runtime import scheduler as sch
    from awgbot.util import timeutil
    _patch_sources(monkeypatch, tmp_path)
    services.backup_set_passphrase(PHRASE)
    for kind in ("assign", "rekey", "token"):
        services.backup_event_mark(kind, 1)
    due = json.loads(services.db.get_state("backup_event_due"))
    assert [w["kind"] for w in due["why"]] == ["assign", "rekey", "token"]
    assert services.backup_event_take() is None, "копия раньше 10 минут после последнего события"
    due["at"] = timeutil.to_iso(timeutil.shift(timeutil.now(), -700))
    services.db.set_state("backup_event_due", json.dumps(due))
    p = tmp_path / "b.tgz.enc"; p.write_bytes(b"x")
    monkeypatch.setattr(services, "make_backup", lambda: [str(p)])

    async def boom(*a, **k):
        raise RuntimeError("линк лежит")
    monkeypatch.setattr(fake_bot, "send_document", boom, raising=False)
    assert asyncio.run(sch.event_backup(services, fake_bot)) is False
    assert services.db.get_state("backup_event_due"), "отметка снята без доставки"
    sent = []

    async def ok(chat, doc, caption=None, **k):
        sent.append(caption)
    monkeypatch.setattr(fake_bot, "send_document", ok, raising=False)
    assert asyncio.run(sch.event_backup(services, fake_bot)) is True
    assert len(sent) == 1 and sent[0].split("\n")[0].endswith("— после изменений шлюзов")
    assert not services.db.get_state("backup_event_due") and services.db.get_state("last_backup") is None


def test_with_autobackups_off_there_are_no_events(services, slot):
    from awgbot.core import settings
    services.backup_set_passphrase(PHRASE)
    settings.set_value("app.scheduler.backup_enabled", False)
    try:
        services.backup_event_mark("rekey", 1)
        assert not services.db.get_state("backup_event_due")
    finally:
        settings.set_value("app.scheduler.backup_enabled", True)


def test_slot_events_mark_a_backup(services, slot, monkeypatch, tmp_path):
    import json
    env = tmp_path / "env"; env.write_text("BOT_TOKEN=DUMMY\n", encoding="utf-8")
    monkeypatch.setenv("AWG_BOT_ENV", str(env))
    services.backup_set_passphrase(PHRASE)
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(services, "_link_privkey", lambda gw=None: "DUMMY")
    services.gateway_token_replace(1, "2222222222:DUMMYDUMMYDUMMYDUMMYDUMMY")
    services.gwcfg_channel_port_tick(8787); services.gwcfg_channel_port_tick(9797)
    admin = services.db.get_device(slot.device_id).client_id
    services.gateway_setup(services.add_device(admin, "Pi-новый").device_id, slot_id=1)
    services.gateway_remove(1)
    kinds = [w["kind"] for w in json.loads(services.db.get_state("backup_event_due"))["why"]]
    assert kinds == ["token", "chport", "replace", "remove"], kinds
    journal = json.loads(services.db.get_state("gw_removed"))
    assert journal[-1]["slot"] == 1 and journal[-1]["link_gw_pub"] == "GWPUB"


# ── правки по ревью: копия по событию, ротация, отметки старта ───────────────

def test_an_undelivered_event_copy_is_reused_and_a_manual_copy_closes_the_event(services, slot, monkeypatch,
                                                                               tmp_path):
    import json
    from awgbot.util import timeutil
    _patch_sources(monkeypatch, tmp_path)
    services.backup_set_passphrase(PHRASE)
    services.backup_event_mark("rekey", 1)
    due = json.loads(services.db.get_state("backup_event_due"))
    first = services.backup_event_paths(due)
    again = services.backup_event_paths(due)
    assert again == first, "недоставленная копия собрана заново"
    later = timeutil.to_iso(timeutil.shift(timeutil.now(), 5))
    services.backup_event_covered(later)
    assert not services.db.get_state("backup_event_due"), "копия, снятая после события, его не закрыла"
    services.backup_event_mark("token", 1)
    services.backup_event_covered(timeutil.to_iso(timeutil.shift(timeutil.now(), -3600)))
    assert services.db.get_state("backup_event_due"), "копия, снятая до события, его закрыла"


def test_the_backup_dir_keeps_the_last_five(services, monkeypatch, tmp_path):
    import os, time as _t
    _patch_sources(monkeypatch, tmp_path)
    services.backup_set_passphrase(PHRASE)
    bk = tmp_path / "backups"; bk.mkdir(exist_ok=True)
    (bk / "restore-pending.tgz.enc").write_bytes(b"x")
    for i in range(7):
        f = bk / f"awg-bot-backup-main-2026010{i}_000000.tgz.enc"
        f.write_bytes(b"x"); os.utime(f, (_t.time() - 1000 + i, _t.time() - 1000 + i))
    services.make_backup()
    left = sorted(p.name for p in bk.glob("awg-bot-backup-*.tgz.enc"))
    assert len(left) == 5 and (bk / "restore-pending.tgz.enc").exists()


def test_the_start_after_a_rolled_back_restore_prunes_once(services, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert not services.restore_done_pending()
    (tmp_path / ".restore-prune").write_text("")
    assert services.restore_done_pending() and not services.restore_done_pending()
    (tmp_path / "restore-failed.json").write_text('{"failed": "бэкап не зашифрован"}', encoding="utf-8")
    assert services.pop_restore_failed() == "бэкап не зашифрован" and services.pop_restore_failed() == ""


def test_links_are_adopted_while_their_interface_is_down(services, slot, monkeypatch, tmp_path):
    runs = []
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: runs.append(mode))
    net = tmp_path / "net"; net.mkdir()
    monkeypatch.setattr(services, "_SYS_NET", str(net))
    assert services.gateway_links_adopt() == ["awglink"] and runs == ["--adopt"]
    (net / "awglink").mkdir()
    assert services.gateway_links_adopt() == [], "поднятый линк принимают снова"
