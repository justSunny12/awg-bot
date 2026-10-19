"""
Бэкап сервера AWG — правки по ревью экранов и строк: подпись файла не длиннее
1024 (сначала — кто не на связи, перечень шлюзов — в оставшееся место, имена
ссылками), отказ Telegram не глотается и не крутит повтор впустую,
всплывашка не длиннее 200; бэкап из чата: не годится — убран из чата, не
похож на бэкап — остаётся; команда терминала в отказе — моноширинным;
кнопка вопроса — того самого файла, повтор во время подготовки — «уже»;
после отчёта о восстановлении — живое меню; профиль в отчёте — ссылкой;
распознавание бэкапа по имени и содержимому (архив выпуска — не бэкап).
"""
from __future__ import annotations

import io
import json
import tarfile

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage

from awgbot.bot import texts
from awgbot.core import config
from awgbot.domain.backup import inspect as ins
from awgbot.domain.notices import backup as nb
from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.integration

PHRASE = "correct horse battery"


def _tar(meta: dict | None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        raw = json.dumps(meta).encode() if meta is not None else b"x"
        ti = tarfile.TarInfo("state/backup-meta.json" if meta is not None else "awg-bot/README")
        ti.size = len(raw)
        tar.addfile(ti, io.BytesIO(raw))
    return buf.getvalue()


# ── подпись файла ────────────────────────────────────────────────────────────

def test_the_caption_fits_1024_and_names_the_offline_first():
    refs = [nb.gw_ref(f"Шлюз-с-очень-длинным-именем-{i:02d}", f"площадка {i:02d}", slot=i, bot_username="b")
            for i in range(1, 16)]
    offline = [(r, "2026-10-12T09:00:00+03:00", "2026-10-12T08:40:00+03:00") for r in refs]
    cap = nb.backup_caption("15.09.26 12:00 (UTC+5)", refs, offline)
    assert len(nb.plain(cap)) <= 1024, len(nb.plain(cap))
    lines = cap.split("\n")
    assert lines[-1].startswith("🟡 И ещё ") and lines[-1].endswith("не на связи — их настройки в бэкапе на дату "
                                                                     "последней связи"), lines[-1]
    assert sum(1 for x in lines if " не на связи с " in x) >= 3, "строки «не на связи» вытеснены перечнем"
    assert 'href="https://t.me/b?start=gw-1"' in cap, "имя шлюза — без ссылки на карточку"
    many = [nb.gw_ref(f"Шлюз-с-очень-длинным-именем-{i:02d}", slot=i) for i in range(1, 41)]
    short = nb.backup_caption("15.09.26 12:00 (UTC+5)", many, [])
    assert len(nb.plain(short)) <= 1024 and " и ещё " in short.split("\n")[1], short
    assert not cap.endswith(".") and not short.endswith(".")


def test_the_service_caption_links_live_slots_only(services, make_active_client):
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    dev = services.add_device(admin.id, "NASPi").device_id
    services.db.gateway_add(dev, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.bot_username = "awg_test_bot"
    cap = services.backup_caption({"why": [{"kind": "rekey", "name": "NASPi", "slot": 1},
                                           {"kind": "remove", "name": "Pi4", "slot": 2}]})
    assert 'start=gw-1">NASPi</a>' in cap and "start=gw-2" not in cap, cap


def test_the_rekey_toast_fits_200():
    t = nb.rekey_wait_toast("Ш" * 400)
    assert len(t.encode("utf-16-le")) // 2 <= 200 and t.endswith("бэкап сниму сразу после неё"), t
    assert nb.rekey_wait_toast("NASPi") == "Идёт смена ключей линка «NASPi» — бэкап сниму сразу после неё"


# ── доставка ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("error, retried", [
    (TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="message caption is too long"), False),
    (TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="timeout"), True),
], ids=["refused", "network"])
async def test_a_refused_file_is_logged_and_not_retried_every_hour(services, tmp_path, error, retried, caplog):
    from awgbot.runtime import scheduler as sch
    services.backup_set_passphrase(PHRASE)
    f = tmp_path / "awg-bot-backup-main-x.tgz.enc"; f.write_bytes(b"DUMMY")
    services.make_backup = lambda: [str(f)]
    services.db.set_state("last_backup", "2000-01")

    class Bot(FakeBot):
        async def send_document(self, *a, **k):
            raise error
    bot = Bot()
    again = []
    await sch.monthly_backup(services, bot, "backup", on_undelivered=lambda: again.append(1))
    ym = __import__("awgbot.util.timeutil", fromlist=["now"]).now().strftime("%Y-%m")
    assert bool(again) is retried
    assert (services.db.get_state("last_backup") == ym) is (not retried)
    notes = [r[2] for r in bot.records if r[0] == "send_message"]
    if not retried:
        assert "повторяться не будет" in caplog.text
        assert notes == ["⚠️ Бэкап сервера AWG не отправлен: message caption is too long — попробуй вручную в "
                         "«⚙️ Настройки → 💾 Бэкапы» или забери из терминала: <code>sudo awg-bot backup</code>"], notes
        await sch.monthly_backup(services, bot, "backup", on_undelivered=lambda: again.append(1))
        assert len([r for r in bot.records if r[0] == "send_message"]) == 1, "уведомление повторилось за месяц"
    else:
        assert notes == [], "сеть, а не отказ — уведомление лишнее"


async def test_a_refused_event_copy_tells_once_and_mail_does_not_duplicate(services, tmp_path, monkeypatch):
    from awgbot.runtime import scheduler as sch
    services.backup_set_passphrase(PHRASE)
    f = tmp_path / "awg-bot-backup-main-x.tgz.enc"; f.write_bytes(b"DUMMY")
    services.backup_event_take = lambda: {"at": "2026-10-17T10:00:00+05:00", "why": []}
    services.backup_event_paths = lambda due: [str(f)]
    done = []
    services.backup_event_done = lambda at: done.append(at)

    class Bot(FakeBot):
        async def send_document(self, *a, **k):
            raise TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="file is too big")
    bot = Bot()
    assert await sch.event_backup(services, bot) is False
    notes = [r[2] for r in bot.records if r[0] == "send_message"]
    assert len(notes) == 1 and notes[0].startswith("⚠️ Бэкап сервера AWG не отправлен: file is too big — ")
    assert done == ["2026-10-17T10:00:00+05:00"] and not notes[0].endswith(".")
    mailed = []
    monkeypatch.setattr(services, "backup_channel", lambda: "email")
    monkeypatch.setattr(services, "email_send_backup", lambda paths, caption="": mailed.append(paths))
    bot2 = Bot()
    assert await sch.event_backup(services, bot2) is True
    assert mailed and [r for r in bot2.records if r[0] == "send_message"] == [], "почтой ушло — а в чат ещё и отказ"


async def test_backup_now_says_when_telegram_refuses_the_file(services, tmp_path, make_active_client):
    from awgbot.bot import sections
    from awgbot.bot.handlers import settingscore as core
    from awgbot.bot.roles import MAIN
    services.backup_set_passphrase(PHRASE)
    f = tmp_path / "awg-bot-backup-main-x.tgz.enc"; f.write_bytes(b"DUMMY")
    services.make_backup = lambda: [str(f)]

    class Msg(FakeMessage):
        async def answer_document(self, document, caption=None, **kw):
            raise RuntimeError("Bad Request: message caption is too long")
    bot = FakeBot()
    msg = Msg(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=bot)
    cb = FakeCallback(message=msg, user_id=config.ADMIN_ID, bot=bot)
    await core.backup_now(cb, services, sections.hooks_for(MAIN))
    said = [t for k, t, *_ in msg.sent if k == "answer"]
    assert "⚠️ Бэкап не отправлен в чат: Bad Request: message caption is too long" in said, msg.sent


# ── бэкап из чата сервера ────────────────────────────────────────────────────

class _Doc:
    def __init__(self, name, size=100):
        self.file_name, self.file_size = name, size


class _DlBot(FakeBot):
    def __init__(self, blob):
        super().__init__()
        self.blob = blob

    async def download(self, doc, destination=None):
        destination.write(self.blob)


async def _offer(services, name, blob, size=100):
    from awgbot.bot.handlers import restore as rs
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=_DlBot(blob))
    msg.document = _Doc(name, size)
    state = FakeState()
    await rs.offer_restore(msg, services, state)
    return msg, state


async def test_a_file_not_like_a_backup_stays_in_the_chat(services):
    msg, _ = await _offer(services, "photos.tgz", _tar(None))
    assert not msg.deleted and msg.sent[-1][1] == texts.restore_rejected("не похоже на бэкап сервера AWG")


async def test_a_rejected_backup_is_removed_from_the_chat(services):
    services.backup_set_passphrase(PHRASE)
    msg, _ = await _offer(services, "awg-bot-backup-main-20261001_120000.tgz.enc", b"AWGBK1P-DUMMY-garbage")
    assert msg.deleted, "отвергнутый бэкап (в нём ключи) остался в чате"
    assert msg.sent[-1][1].startswith("⚠️ Файл не принят: не удалось расшифровать — ключевая фраза не подходит")
    assert "<code>sudo awg-bot restore &lt;файл&gt;</code>" in msg.sent[-1][1]
    msg, _ = await _offer(services, "old.tgz", _tar({"role": "main", "created_at": "2026-09-01T00:00:00+05:00"}))
    assert msg.deleted and msg.sent[-1][1] == texts.restore_rejected(ins.OPEN_REFUSAL), "открытый бэкап с ключами"


async def test_a_too_big_backup_points_to_the_terminal(services):
    msg, _ = await _offer(services, "awg-bot-backup-main-x.tgz.enc", b"", size=30 * 1024 * 1024)
    assert msg.deleted
    assert msg.sent[-1][1] == ("⚠️ Файл не принят: бэкап больше 20 МБ — из чата бот его не скачает; восстанови в "
                               "терминале сервера: <code>sudo awg-bot restore &lt;файл&gt;</code>")
    msg, _ = await _offer(services, "video.tar.gz", b"", size=30 * 1024 * 1024)
    assert not msg.deleted, "чужой большой файл убран из чата"


def test_another_admin_command_is_monospace():
    text = texts.restore_rejected("бэкап другого админа — из чата его не восстановить; на сервере: "
                                  "sudo awg-bot restore <файл>")
    assert text.endswith("на сервере: <code>sudo awg-bot restore &lt;файл&gt;</code>"), text


# ── кнопка вопроса — того самого файла ───────────────────────────────────────

async def test_the_button_is_of_its_own_file_and_a_second_tap_is_refused(services):
    from awgbot.bot.handlers import restore as rs
    state = FakeState()
    await state.update_data(restore_blob="RFVNTVk=", restore_plan={}, restore_fp="aaaaaaaaaaaa")
    bot = FakeBot()
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=bot)
    cb = FakeCallback(message=msg, user_id=config.ADMIN_ID, bot=bot)
    await rs.run_restore(cb, services, state, "bbbbbbbbbbbb")
    assert cb.answers[-1][0] == texts.RESTORE_OTHER_FILE and (await state.get_data())["restore_blob"]
    launched = []
    services.prepare_restore = lambda blob, plan=None: "/tmp/DUMMY"
    services.launch_restore = lambda path, release="": launched.append(path)
    await rs.run_restore(cb, services, state, "aaaaaaaaaaaa")
    assert launched == ["/tmp/DUMMY"]
    await state.update_data(restore_blob="RFVNTVk=", restore_fp="aaaaaaaaaaaa")
    await rs.run_restore(cb, services, state, "aaaaaaaaaaaa")
    assert cb.answers[-1][0] == texts.RESTORE_ALREADY and launched == ["/tmp/DUMMY"]


# ── после восстановления ─────────────────────────────────────────────────────

async def test_the_report_is_followed_by_a_live_menu(services, fake_bot):
    from awgbot.bot.handlers import restore as rs
    services.pop_restore_failed = lambda: ""
    services.pop_restore_done = lambda: {"created_at": "2026-10-05T14:00:00+03:00",
                                         "merge": {"line": "same", "slots": [], "lost": []}}
    await rs.report_restore_result(fake_bot, services)
    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 2 and sent[0][2].startswith("✅ Восстановлено из бэкапа"), sent
    assert services.db.get_nav_message_id(config.ADMIN_ID), "под отчётом нет живого меню"


def test_a_lost_device_names_its_profile_as_a_link():
    rep = {"slots": [], "lost": [{"device": "Телефон", "client": "Иван", "address": "10.8.1.7",
                                  "client_id": 5, "client_tg": 2001}]}
    text = nb.restore_report("2026-10-05T14:00:00+03:00", rep, bot_username="awg_test_bot")
    assert 'профиля <a href="https://t.me/awg_test_bot?start=cl-5">Иван</a>' in text, text
    assert "профиля Иван" in nb.restore_report("2026-10-05T14:00:00+03:00", rep)


# ── распознавание бэкапа ─────────────────────────────────────────────────────

@pytest.mark.parametrize("name, blob, kind", [
    ("awg-bot-backup-main-20261017.tgz.enc", None, "main"),
    ("awg-bot-backup-gw-20261017.tgz.enc", None, "gw"),
    ("awg-bot-gw_20260901.tgz", None, "gw"),
    ("awg-bot.tgz", None, ""),
    ("awg-bot.tgz", _tar(None), ""),
    ("whatever.bin", b"AWGBK1DUMMY", "any"),
    ("x.tgz", _tar({"role": "gw"}), "gw"),
    ("x.tgz", _tar({"role": "main"}), "main"),
    ("notes.tgz.enc", b"DUMMY-not-a-backup", ""),
], ids=["main-name", "gw-name", "gw-old-name", "release-name", "release", "renamed-enc", "gw-label", "main-label",
        "junk"])
def test_recognize(name, blob, kind):
    assert ins.recognize(name, blob) == kind


def test_the_empty_server_notice_needs_no_file_or_command():
    """Пустой сервер после восстановления: путь подключения — кнопками в боте
    (Т-П17), ни файла конфигурации, ни команды gw-bundle больше нет."""
    for link_if in ("awglink", "awglink2"):
        text = nb.gateway_not_back("x", empty=True, link_if=link_if)
        assert "«🔁 Настроить заново»" in text and "gw-bundle" not in text and "<code>" not in text
