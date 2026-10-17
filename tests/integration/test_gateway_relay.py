"""
Вторая точка входа восстановления: бэкап сервера AWG, присланный в чат бота
шлюза, агент передаёт серверу по каналу — частями, с контрольной суммой и
пределом размера (domain/backup/relay, linkclient/relay, linkserver/relay), а
сервер дальше идёт штатными рельсами: осмотр, вопрос в чате бота сервера
(handlers/restore.relay_restore). Итог — правкой «⏳ Передаю…» в чате бота
шлюза: принят — где вопрос (ссылкой на бота сервера), отказ — словами сервера,
обрыв — «пришли файл снова». На шлюзе ничего не хранится; собранное у сервера
живёт в сессии и пропадает с ней.

Канал подставной: части клиента уходят прямо в приём сервера, ответ сервера —
прямо в разбор клиента. Сокета нет; нарезка, сборка и решения — настоящие.
"""
from __future__ import annotations

import asyncio
import base64
import os

import pytest

from awgbot.bot import texts
from awgbot.core import config
from awgbot.domain.backup import relay
from awgbot.runtime import linkclient, linkserver
from awgbot.runtime.linkserver.common import _Session
from awgbot.runtime.linkserver.relay import RelayMixin
from tests.conftest import FakeBot, FakeMessage, FakeState

pytestmark = pytest.mark.integration

SERVER_BOT = {"username": "awg_srv_bot"}


# ── нарезка и сборка ─────────────────────────────────────────────────────────

def _blob(n: int) -> bytes:
    return b"AWGBK1" + os.urandom(n - 6)


def test_parts_assemble_in_any_order_and_a_repeated_part_is_harmless():
    blob = _blob(relay.PART * 2 + 1000)
    parts = relay.split(blob, "awg-bot-backup-main-x.tgz.enc")
    assert len(parts) == 3 and all(p["n"] == 3 and p["id"] == parts[0]["id"] for p in parts)
    asm = relay.Assembly(parts[2])
    for p in (parts[2], parts[0], parts[0], parts[1]):              # повтор части — та же часть
        asm.add(p)
    assert asm.done and asm.blob() == blob and asm.name == "awg-bot-backup-main-x.tgz.enc"


@pytest.mark.parametrize("spoil, why", [
    (lambda p: {**p, "z": base64.b64encode(b"x" * 11).decode()}, "часть не того размера"),
    (lambda p: {**p, "i": 7}, "часть вне передачи"),
    (lambda p: {**p, "size": p["size"] + 1}, "части разных передач"),
], ids=["size", "index", "mixed"])
def test_a_bad_part_is_refused(spoil, why):
    parts = relay.split(_blob(relay.PART + 10), "b.tgz.enc")
    asm = relay.Assembly(parts[0])
    with pytest.raises(relay.RelayError, match=why):
        asm.add(spoil(parts[1]))


def test_limits_and_checksum():
    with pytest.raises(relay.RelayError, match="больше 20 МБ"):
        relay.split(b"x" * (relay.MAX_BYTES + 1), "b")
    with pytest.raises(relay.RelayError, match="больше 20 МБ"):
        relay.Assembly({"id": "a1b2c3d4e5f6", "sha": "ab" * 32, "size": relay.MAX_BYTES + 1, "n": 999})
    parts = relay.split(_blob(100), "b")
    asm = relay.Assembly({**parts[0], "sha": "cd" * 32})
    asm.add({**parts[0], "sha": "cd" * 32})
    with pytest.raises(relay.RelayError, match="повреждённым"):
        asm.blob()


# ── канал: агент ↔ сервер ────────────────────────────────────────────────────

class _Gw:
    id = 1


class _Server(RelayMixin):
    """Приём сервера: сессия слота, ответы — в разбор клиента."""

    def __init__(self, client):
        self.sess = _Session(writer=None, key=b"")
        self.client = client
        self.answers = []

    async def send(self, slot_id, kind, body=None, pad=0):
        self.answers.append((kind, body))
        if kind == "rst_done":
            self.client._rst_done(body)
        return True


class _Agent:
    def __init__(self, tmp_path, caps=("restore1",)):
        from awgbot.domain.gateway import GatewayServices
        from awgbot.infra.db import Database
        from awgbot.runtime.linkclient import LinkClient
        self.db = Database(str(tmp_path / "gw.db")); self.db.init_schema()
        self.s = GatewayServices(self.db)
        self.s.set_link_role(True, caps=list(caps), bot=SERVER_BOT["username"])
        self.c = LinkClient(self.s)
        self.c._writer = object()
        self.srv = _Server(self.c)
        self.parts = []
        self.break_at = None

        async def send(kind, body=None, pad=0):
            if kind != "rst_part":
                return True
            self.parts.append(body["i"])
            if self.break_at is not None and len(self.parts) > self.break_at:
                self.c._writer = None
                self.c._rst_broken()
                return False
            await self.srv._rst_part(_Gw(), self.srv.sess, body)
            return True
        self.c._send = send


@pytest.fixture()
def hook(monkeypatch):
    got = []

    async def on_restore(slot, blob, name):
        got.append((slot, blob, name))
        return got[-1] and getattr(on_restore, "answer", "")
    monkeypatch.setattr(linkserver, "_on_restore", on_restore)
    return on_restore, got


def test_a_backup_goes_in_parts_and_the_server_takes_it_on_its_rails(tmp_path, hook):
    on_restore, got = hook
    a = _Agent(tmp_path)
    blob = _blob(relay.PART * 2 + 5)
    res = asyncio.run(a.c.send_backup(blob, "awg-bot-backup-main-x.tgz.enc"))
    assert res == {"ok": True, "why": "", "error": ""}
    assert a.parts == [0, 1, 2] and got == [(1, blob, "awg-bot-backup-main-x.tgz.enc")]
    assert a.srv.sess.rst is None, "собранное осталось в сессии"
    text = texts.gw_relay_result(res, a.s.link_server_bot())
    assert text == ('✅ Бэкап передан серверу AWG: вопрос о восстановлении — в чате '
                    '<a href="https://t.me/awg_srv_bot">бота сервера AWG</a>')
    a.db.close()


def test_a_server_refusal_comes_back_in_its_words(tmp_path, hook):
    on_restore, got = hook
    on_restore.answer = "бэкап снят версией до 3.4.0 — восстановиться из него не получится"
    a = _Agent(tmp_path)
    res = asyncio.run(a.c.send_backup(_blob(500), "b.tgz.enc"))
    assert res["why"] == "refused" and res["error"] == on_restore.answer
    assert texts.gw_relay_result(res, {}) == ("⚠️ Бэкап не принят сервером AWG: бэкап снят версией до 3.4.0 — "
                                              "восстановиться из него не получится")
    a.db.close()


def test_a_break_mid_transfer_says_send_again_and_leaves_nothing(tmp_path, hook):
    on_restore, got = hook
    a = _Agent(tmp_path)
    a.break_at = 1
    res = asyncio.run(a.c.send_backup(_blob(relay.PART * 3), "b.tgz.enc"))
    assert res["why"] == "broken" and got == [], "сервер получил недособранный бэкап"
    assert texts.gw_relay_result(res, SERVER_BOT).startswith(
        "⚠️ Не передал: связь с сервером AWG оборвалась — пришли файл снова (или в чат ")
    assert not list(tmp_path.glob("*.tgz*")), "на шлюзе остался файл бэкапа"
    a.srv.sess = _Session(writer=None, key=b"")                    # новая сессия — с чистого листа
    assert a.srv.sess.rst is None
    a.break_at, a.c._writer, a.parts = None, object(), []
    blob = _blob(relay.PART + 7)
    assert asyncio.run(a.c.send_backup(blob, "b.tgz.enc"))["ok"], "новый файл после обрыва не прошёл"
    assert got[-1][1] == blob
    a.db.close()


def test_a_break_while_waiting_for_the_answer(tmp_path, hook):
    """Все части ушли, а сессия кончилась до ответа сервера — «оборвалась»."""
    a = _Agent(tmp_path)

    async def lost(slot_id, kind, body=None, pad=0):
        a.c._writer = None
        a.c._rst_broken()
        return False
    a.srv.send = lost
    res = asyncio.run(a.c.send_backup(_blob(100), "b.tgz.enc"))
    assert res["why"] == "broken"
    a.db.close()


def test_a_new_transfer_replaces_an_unfinished_one(tmp_path, hook):
    on_restore, got = hook
    a = _Agent(tmp_path)
    first = relay.split(_blob(relay.PART + 3), "old.tgz.enc")
    asyncio.run(a.srv._rst_part(_Gw(), a.srv.sess, first[0]))       # первая часть прежней передачи
    blob = _blob(200)
    assert asyncio.run(a.c.send_backup(blob, "new.tgz.enc"))["ok"]
    assert got == [(1, blob, "new.tgz.enc")]
    a.db.close()


def test_a_server_without_restore1_is_not_asked(tmp_path, hook):
    a = _Agent(tmp_path, caps=("cfg1", "keep1"))
    res = asyncio.run(a.c.send_backup(_blob(100), "b.tgz.enc"))
    assert res["why"] == "no_cap" and a.parts == []
    assert "сервер AWG старше 3.4.0 и бэкапы через шлюз не принимает" in texts.gw_relay_result(res, SERVER_BOT)
    a.db.close()


def test_without_a_session_or_a_client_it_is_offline(tmp_path):
    a = _Agent(tmp_path)
    a.c._writer = None
    assert asyncio.run(a.c.send_backup(b"x", "b"))["why"] == "offline"
    assert asyncio.run(linkclient.relay_backup(b"x", "b"))["why"] == "offline"
    text = texts.gw_relay_result({"ok": False, "why": "offline"}, SERVER_BOT)
    assert text.startswith("⚠️ Бэкап не передан: сервер AWG сейчас не на связи — пришли бэкап в чат ")
    assert text.endswith("или восстанови на сервере: <code>sudo awg-bot restore &lt;файл&gt;</code>")
    a.db.close()


def test_a_server_without_the_hook_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(linkserver, "_on_restore", None)
    a = _Agent(tmp_path)
    res = asyncio.run(a.c.send_backup(_blob(100), "b.tgz.enc"))
    assert res["why"] == "refused" and "не принимает бэкапы через шлюз" in res["error"]
    a.db.close()


def test_the_server_bot_name_comes_with_role(tmp_path):
    a = _Agent(tmp_path)
    assert a.s.link_server_bot() == SERVER_BOT
    a.s.set_link_role(True, bot="bad name!")
    assert a.s.link_server_bot() == {}
    a.db.close()


# ── чат бота шлюза ───────────────────────────────────────────────────────────

class _Doc:
    def __init__(self, name, size=100):
        self.file_name, self.file_size, self.file_id = name, size, "F"


class _DlBot(FakeBot):
    def __init__(self, blob):
        super().__init__()
        self.blob = blob

    async def download(self, doc, destination=None):
        destination.write(self.blob)


@pytest.mark.parametrize("res", [{"ok": True, "why": "", "error": ""},
                                 {"ok": False, "why": "broken", "error": ""},
                                 {"ok": False, "why": "refused", "error": "бэкап другого админа — из чата его не "
                                  "восстановить; на сервере: sudo awg-bot restore <файл>"}],
                         ids=["accepted", "broken", "refused"])
async def test_the_gateway_chat_says_sending_then_the_outcome(tmp_path, monkeypatch, res):
    from awgbot.bot.handlers import gateway as gh
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    d = Database(str(tmp_path / "gw.db")); d.init_schema()
    svc = GatewayServices(d)
    svc.set_link_role(True, caps=["restore1"], bot="awg_srv_bot")
    sent = []

    async def fake_relay(blob, name):
        sent.append((blob, name))
        return res
    monkeypatch.setattr(linkclient, "relay_backup", fake_relay)
    bot = _DlBot(b"AWGBK1DUMMY")
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=bot)
    msg.document = _Doc("awg-bot-backup-main-20261017_120000.tgz.enc")
    await gh.gw_bundle_document(msg, svc, FakeState())
    assert msg.deleted, "бэкап с ключами остался в чате шлюза"
    assert sent == [(b"AWGBK1DUMMY", "awg-bot-backup-main-20261017_120000.tgz.enc")]
    said = [r for r in bot.records if r[0] in ("answer", "edit_text")]
    assert said[0] == ("answer", config.ADMIN_ID, "⏳ Передаю бэкап серверу AWG…")
    assert said[-1] == ("edit_text", config.ADMIN_ID, texts.gw_relay_result(res, SERVER_BOT)), said
    if res["why"] == "refused":
        assert said[-1][2].endswith("на сервере: <code>sudo awg-bot restore &lt;файл&gt;</code>")
    d.close()


async def test_a_too_big_server_backup_is_refused_without_download(tmp_path):
    from awgbot.bot.handlers import gateway as gh
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    d = Database(str(tmp_path / "gw.db")); d.init_schema()

    class NoDl(FakeBot):
        async def download(self, doc, destination=None):
            raise AssertionError("скачан файл крупнее предела")
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=NoDl())
    msg.document = _Doc("awg-bot-backup-main-x.tgz.enc", size=30 * 1024 * 1024)
    await gh.gw_bundle_document(msg, GatewayServices(d), FakeState())
    assert msg.deleted and msg.sent[-1][1] == ("⚠️ Бэкап не передан: он больше 20 МБ — восстанови на сервере: "
                                               "<code>sudo awg-bot restore &lt;файл&gt;</code>")
    d.close()


async def test_a_file_from_someone_else_is_not_relayed(tmp_path, monkeypatch):
    from awgbot.bot.handlers.gateway import bundle as gb
    called = []

    async def fake_relay(blob, name):
        called.append(1)
        return {"ok": True}
    monkeypatch.setattr(linkclient, "relay_backup", fake_relay)
    msg = FakeMessage(chat_id=999, user_id=999, bot=FakeBot())
    await gb._relay(msg, object(), b"AWGBK1", "b.tgz.enc")
    assert called == [] and msg.sent == []


# ── сервер: штатные рельсы ───────────────────────────────────────────────────

async def test_the_server_asks_in_its_own_chat_and_replaces_a_previous_question(services, fake_bot):
    from awgbot.bot.handlers import restore as rs
    services.inspect_backup = lambda blob, name="": {"ok": True, "created_at": "2026-10-05T14:00:00+03:00",
                                                     "plan": {"line": "same", "slots": [], "created_at": ""}}
    state = FakeState()
    await state.update_data(restore_qmid=4242)                      # висел вопрос о прежнем файле
    edits = []

    async def edit(text, chat_id=None, message_id=None, **kw):
        edits.append((message_id, text))
    fake_bot.edit_message_text = edit
    assert await rs.relay_restore(fake_bot, services, state, b"AWGBK1DUMMY", "b.tgz.enc") == ""
    data = await state.get_data()
    assert data["restore_blob"] and data["restore_fp"] and data["restore_qmid"] != 4242
    assert edits == [(4242, texts.RESTORE_REPLACED)]
    asked = [r for r in fake_bot.records if r[0] == "send_message"]
    assert asked and asked[-1][1] == config.ADMIN_ID and asked[-1][2].startswith("♻️ Восстановить бэкап от ")


async def test_a_server_refusal_says_nothing_in_its_own_chat(services, fake_bot):
    from awgbot.bot.handlers import restore as rs
    services.backup_set_passphrase("correct horse battery")
    err = await rs.relay_restore(fake_bot, services, FakeState(), b"AWGBK1-not-a-backup", "b.tgz.enc")
    assert err.startswith("не удалось расшифровать — ключевая фраза не подходит")
    assert [r for r in fake_bot.records if r[0] == "send_message"] == []
