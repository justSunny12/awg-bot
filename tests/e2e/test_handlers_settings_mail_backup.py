"""E2E: почта и резервные копии из чата — мастер подключения ящика, бэкап на
почту и запасной канал алертов, парольная фраза шифрования, восстановление
из файла в чате."""

import pytest

from awgbot.bot.handlers import admin as ah
from tests.conftest import FakeState

pytestmark = pytest.mark.e2e


# ── ✉️ E-mail: мастер подключения из чата ────────────────────────────────────

def _email_store(monkeypatch):
    from awgbot.core import settings
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: bool(store.get(k, d)))
    return store


async def test_email_wizard_known_provider_saves_after_live_check(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    store = _email_store(monkeypatch)
    checked = []
    monkeypatch.setattr(services, "email_check", lambda acc=None: (checked.append(acc), (True, "ок"))[1])
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    state = FakeState()
    await sh.email_action(cb, SetCB(sec="email", act="do", key="setup"), services, state)
    assert any("Подключение ящика" in t for kind, t, _ in msg.sent if kind == "edit_text")
    addr = FakeMessage(text="box@icloud.com", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.email_address(addr, state, services)
    assert any("Провайдер распознан" in t and "app-specific" in t for kind, t, _ in addr.sent)
    pw = FakeMessage(text="s3cret", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.email_password(pw, state, services)
    assert any(r[0] == "delete" for r in fake_bot.records), "сообщение с паролем не удалено"
    assert checked and checked[0].password == "s3cret" and checked[0].imap_host == "imap.mail.me.com"
    assert services.db.get_state("email_login") == "box@icloud.com"
    assert store["email.smtp_host"] == "smtp.mail.me.com"
    assert any("подключён" in t for kind, t, _ in pw.sent)
    assert await state.get_data() == {}
    # Раздел после мастера — через send_menu: живое меню сместилось с вопроса,
    # приглашение и адрес человека убраны (раньше раздел уходил голым answer)
    assert services.db.get_nav_message_id(cfg.ADMIN_ID) != msg.message_id
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert msg.message_id in deleted and addr.message_id in deleted
    section = [x for x in pw.sent if x[0] == "answer"][-1]
    assert section[2] is not None and "подключён" not in section[1]


async def test_email_wizard_unknown_domain_asks_servers_and_failed_check_saves_nothing(
        services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    _email_store(monkeypatch)
    monkeypatch.setattr(services, "email_check", lambda acc=None: (False, "IMAP отверг логин/пароль"))
    state = FakeState(); await state.set_state("x")
    m = lambda t: FakeMessage(text=t, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    a = m("box@corp.example"); await sh.email_address(a, state, services)
    assert any("IMAP-сервер" in t for kind, t, _ in a.sent)
    await sh.email_imap_host(m("imap.corp.example"), state, services)
    bad = m("99999"); await sh.email_imap_port(bad, state, services)
    assert any("порта" in t for kind, t, _ in bad.sent)
    await sh.email_imap_port(m("993"), state, services)
    await sh.email_smtp_host(m("smtp.corp.example"), state, services)
    await sh.email_smtp_port(m("587"), state, services)
    pw = m("pw"); await sh.email_password(pw, state, services)
    assert any("Не подключено" in t and "IMAP отверг" in t for kind, t, _ in pw.sent)
    assert not services.db.get_state("email_login")


async def test_email_forget_needs_confirmation_and_toggle_resume(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    store = _email_store(monkeypatch)
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    from tests.conftest import FakeState
    await sh.email_action(cb, SetCB(sec="email", act="do", key="forget"), services, FakeState())
    assert services.email_account() is not None
    assert any("Отключить почту?" in t for kind, t, _ in msg.sent if kind == "edit_text")
    await sh.toggle(cb, SetCB(sec="email", act="toggle", key="email.resume_enabled"), services)
    assert store["email.resume_enabled"] is False
    await sh.email_action(cb, SetCB(sec="email", act="do", key="forget!"), services, FakeState())
    assert services.email_account() is None
    assert any("Почта отключена" in t for kind, t, _ in msg.sent if kind == "answer")


# ── бэкап на почту и запасной канал для критичных алертов ────────────────────

async def test_backup_channel_email_requires_mailbox_and_encryption(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    store = _email_store(monkeypatch)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.pick(cb, SetCB(sec="backup", act="pick", key="channel", val="email"), services)
    assert any("Почта не настроена" in t for kind, t, _ in msg.sent if kind == "edit_text")
    assert "app.scheduler.backup_channel" not in store
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    await sh.pick(cb, SetCB(sec="backup", act="pick", key="channel", val="email"), services)
    assert "app.scheduler.backup_channel" not in store, "без шифрования почтовый канал не включается"
    services.backup_set_passphrase("correct horse battery")
    await sh.pick(cb, SetCB(sec="backup", act="pick", key="channel", val="email"), services)
    assert store["app.scheduler.backup_channel"] == "email"
    # «создать сейчас» уходит письмом
    mailed = []
    monkeypatch.setattr(services, "make_backup", lambda: ["/tmp/a.enc", "/tmp/b.enc"])
    monkeypatch.setattr(services, "email_send_backup", lambda paths: mailed.append(paths))
    await sh.do_action(cb, SetCB(sec="backup", act="do", key="now"), services)
    assert mailed == [["/tmp/a.enc", "/tmp/b.enc"]]
    assert any("отправлена на ящик" in t and "box@icloud.com" in t for kind, t, _ in msg.sent if kind == "answer")


async def test_email_fallback_toggle_offers_setup_without_mailbox(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage
    import awgbot.core.config as cfg
    store = _email_store(monkeypatch)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.toggle(cb, SetCB(sec="notify", act="toggle", key="notifications.email_fallback"), services)
    assert any("Почта не настроена" in t for kind, t, _ in msg.sent if kind == "edit_text")
    assert "notifications.email_fallback" not in store
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    await sh.toggle(cb, SetCB(sec="notify", act="toggle", key="notifications.email_fallback"), services)
    assert store["notifications.email_fallback"] is True


async def test_critical_alert_goes_to_email_when_telegram_is_down(monkeypatch):
    from aiogram.exceptions import TelegramNetworkError
    from awgbot.bot import notifier
    from awgbot.domain.services import Notification
    import awgbot.core.config as cfg

    class DeadBot:
        async def send_message(self, *a, **k):
            raise TelegramNetworkError(method=None, message="network down")

    mailed = []

    async def fb(text):
        mailed.append(text)
    notifier.set_email_fallback(fb)
    try:
        await notifier.send_notifications(DeadBot(), [
            Notification(cfg.ADMIN_ID, "обычное", critical=False),
            Notification(cfg.ADMIN_ID, "🚨 сервис лежит", critical=True),
            Notification(12345, "🚨 чужой критичный", critical=True),
        ])
    finally:
        notifier.set_email_fallback(None)
    assert mailed == ["🚨 сервис лежит"], "на почту — только критичное и только админу"


# ── 🔐 шифрование бэкапов из чата ────────────────────────────────────────────

async def test_backup_passphrase_flow_deletes_messages_and_requires_match(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from awgbot.bot import keyboards as kbs
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    _email_store(monkeypatch)
    rows = [[b.text for b in r] for r in kbs.settings_backup(False).inline_keyboard]
    assert rows[1] == ["🔐 Шифрование: 🔴 выключено"], rows
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await sh.do_action(cb, SetCB(sec="backup", act="do", key="enc"), services)
    assert any("Шифрование резервных копий" in t for kind, t, _ in msg.sent if kind == "edit_text")
    state = FakeState()
    await sh.backup_passphrase_start(cb, state, services)
    m = lambda t: FakeMessage(text=t, chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    short = m("abc"); await sh.backup_passphrase_first(short, state, services)
    assert any("короче" in t for kind, t, _ in short.sent)
    await sh.backup_passphrase_first(m("correct horse battery"), state, services)
    wrong = m("correct horse batery"); await sh.backup_passphrase_second(wrong, state, services)
    assert any("не совпали" in t for kind, t, _ in wrong.sent) and not services.backup_encryption_enabled()
    await sh.backup_passphrase_first(m("correct horse battery"), state, services)
    ok = m("correct horse battery"); await sh.backup_passphrase_second(ok, state, services)
    assert services.backup_enc_kwargs() == {"passphrase": "correct horse battery"}
    deletes = [r for r in fake_bot.records if r[0] == "delete"]
    assert len(deletes) >= 4, "сообщения с фразой должны удаляться"
    assert not any("correct horse" in t for kind, t, _ in ok.sent), "фраза не должна печататься обратно"
    assert [[b.text for b in r] for r in kbs.settings_backup(True).inline_keyboard][1] == ["🔐 Шифрование: ✅ включено"]


# ── ♻️ восстановление из файла в чате ────────────────────────────────────────

async def test_backup_file_in_chat_offers_restore_and_confirm_launches(services, fake_bot, monkeypatch, tmp_path):
    import io, json, tarfile
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    monkeypatch.setattr(cfg, "ROLE", "client")
    monkeypatch.setattr(cfg, "BACKUP_DIR", tmp_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        raw = json.dumps({"role": "main", "created_at": "2026-09-09T10:30:00+03:00"}).encode()
        ti = tarfile.TarInfo("state/backup-meta.json"); ti.size = len(raw); tar.addfile(ti, io.BytesIO(raw))
    blob = buf.getvalue()

    class DlBot(FakeBot):
        async def download(self, doc, destination=None):
            destination.write(blob)
    bot = DlBot()
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    msg.document = type("D", (), {"file_name": "awg-bot-backup-main-x.tgz", "file_size": len(blob), "file_id": "F"})()
    state = FakeState()
    await ah.admin_document(msg, services, state)
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "бэкап настроек бота и сервиса от 09.09.2026 10:30" in sent[-1] and "Важно!" in sent[-1]
    # в копии вся база — сообщение с ней из чата убираем, как и присланный токен
    assert msg.deleted
    launched = []
    monkeypatch.setattr(services, "launch_restore", lambda path: launched.append(path))
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=bot)
    await sh.backup_restore_action(cb, SetCB(sec="backup", act="do", key="restore!"), services, state)
    assert launched and launched[0].endswith("restore-pending.tgz")
    assert (tmp_path / "restore-pending.tgz").read_bytes() == blob
    assert any("Восстанавливаю" in t for kind, t, _ in msg.sent if kind == "answer")
    assert await state.get_data() == {}


async def test_foreign_role_backup_is_rejected_in_chat(services, fake_bot, monkeypatch):
    import io, json, tarfile
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeBot, FakeMessage, FakeState
    import awgbot.core.config as cfg
    monkeypatch.setattr(cfg, "ROLE", "client")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        raw = json.dumps({"role": "gw", "created_at": "2026-09-09T10:30:00+03:00"}).encode()
        ti = tarfile.TarInfo("state/backup-meta.json"); ti.size = len(raw); tar.addfile(ti, io.BytesIO(raw))
    blob = buf.getvalue()

    class DlBot(FakeBot):
        async def download(self, doc, destination=None):
            destination.write(blob)
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=DlBot())
    msg.document = type("D", (), {"file_name": "b.tgz", "file_size": len(blob), "file_id": "F"})()
    await ah.admin_document(msg, services, FakeState())
    assert any("копия агента шлюза" in t for kind, t, _ in msg.sent if kind == "answer")
