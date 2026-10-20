"""
Бэкап — правки по вычитке строк: «ключевая фраза» эталоном, «не теряй»,
ссылки на чат бота шлюза в новых строках, план и отказы дословно; ветки
диалогов не оставляют живых кнопок в устаревших сообщениях: вопрос о
восстановлении, заменённый новым файлом, и вопрос о настройках шлюза,
снятый сервером, правятся в финишер без кнопок.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from awgbot.bot import texts
from awgbot.bot.roles import MAIN
from awgbot.core import config
from awgbot.domain.notices import backup as nb
from tests.conftest import FakeBot, FakeMessage, FakeState

pytestmark = pytest.mark.integration

BOT = {"username": "naspi_gw_bot", "name": "NASPi"}


def test_the_key_phrase_is_the_one_name():
    assert texts.backup_encryption_text("", MAIN).startswith(
        "🔐 <b>Шифрование бэкапов</b> 🔓 ключевая фраза не задана — бэкапы не делаются")
    assert "🔐 ключевая фраза задана" in texts.backup_encryption_text("passphrase", MAIN)
    details = texts.backup_encryption_text("passphrase", MAIN)
    assert ("Смена ключевой фразы не перешифровывает старые бэкапы: из чата бот расшифровывает только текущей "
            "фразой, а из бэкапа с прежней фразой можно восстановиться в терминале сервера — "
            "<code>sudo awg-bot restore &lt;файл&gt;</code>. Не теряй прежнюю фразу, пока такие бэкапы нужны") in details
    assert "выбрасыва" not in details
    assert texts.BACKUP_ASK_PASSPHRASE.startswith("🔐 <b>Ключевая фраза</b>")
    assert texts.BACKUP_PASSPHRASE_SET.startswith("✅ Ключевая фраза задана")
    assert texts.settings_backup_text(False, "telegram", MAIN).startswith("💾 <b>Бэкапы</b> · 🔓 ключевая фраза не задана")


def test_the_plan_and_refusals_are_word_for_word():
    from awgbot.domain.backup import meta
    from awgbot.domain.backup.plan import Plan
    p = Plan.from_dict({"line": "foreign", "created_at": "2026-10-05T14:00:00+03:00", "replaced": [3, 0],
                        "slots": [], "empty": False, "expired_now": 0, "host_copy": "", "host_cur": "",
                        "first_slot_back": False})
    text = nb.restore_plan(p)
    assert ("⚠️ Бэкап снят на другом сервере: текущие профили (3) будут удалены и потеряют доступ, их место займут "
            "профили и устройства из бэкапа. Шлюзы этого сервера останутся как есть") in text, text
    u = Plan.from_dict({**p.to_dict(), "line": "unreadable"})
    assert "⚠️ Текущая база не читается — сопоставлять не с чем: всё возьму из бэкапа" in nb.restore_plan(u)
    assert "из бэкапа." not in nb.restore_plan(u), "точка в конце строки без слотов"
    assert meta.check({"schema": 1, "role": "main"}) == "бэкап снят версией до 4.0.0 — восстановиться из него не получится"
    assert texts.restore_failed("x") == "⚠️ Восстановление из бэкапа не удалось: x"
    assert nb.backup_refused_notice("r").startswith("⚠️ Бэкап сервера AWG не отправлен: r — попробуй вручную в ")


def test_chat_references_are_links_to_the_gateway_bot():
    from awgbot.bot.texts.routing.card import keep_line
    assert keep_line({"state": "asked", "asked": "device"}, BOT) == (
        '⏳ Настройки прежнего устройства ждут решения в чате <a href="https://t.me/naspi_gw_bot">бота шлюза</a>')
    assert keep_line({"state": "asked", "asked": "device"}) .endswith("в чате бота шлюза"), "без бота — текстом"
    assert '<a href="https://t.me/naspi_gw_bot">бота шлюза</a>' in nb.gateway_not_back("x", empty=False, bot=BOT)
    assert nb.chat_ref({"username": "bad name!"}) == "бота шлюза"


async def test_a_new_file_turns_the_previous_question_into_a_finisher(services, monkeypatch):
    from awgbot.bot.handlers import restore as rs
    services.inspect_backup = lambda blob, name="": {"ok": True, "created_at": "2026-10-05T14:00:00+03:00",
                                                     "plan": {"line": "same", "slots": [], "created_at": ""}}
    edits = []

    class Bot(FakeBot):
        async def download(self, doc, destination=None):
            destination.write(b"AWGBK1DUMMY")

        async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
            edits.append((message_id, text, kw.get("reply_markup")))

    class Doc:
        file_name, file_size = "awg-bot-backup-main-x.tgz.enc", 100
    bot, state = Bot(), FakeState()
    first = None
    for _ in range(2):
        msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=bot)
        msg.document = Doc()
        await rs.offer_restore(msg, services, state)
        first = first or (await state.get_data())["restore_qmid"]
    assert first and len(edits) == 1 and edits[0][0] == first, (first, edits)
    assert edits[0][1].startswith("<b>Не актуально: прислан новый файл бэкапа</b>\n\n<blockquote expandable>♻️ ")
    assert [b.text for r in edits[0][2].inline_keyboard for b in r] == ["Скрыть"]
    assert (await state.get_data())["restore_qmid"] != first, "новый вопрос не запомнен"


def test_a_withdrawn_keep_question_becomes_a_finisher(tmp_path, monkeypatch):
    """Сервер снял предложение (keep_ok), а вопрос висел в чате: сообщение с
    вопросом правится в финишер без кнопок."""
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    from awgbot.runtime import linkclient
    from awgbot.runtime.linkclient import LinkClient
    from awgbot.runtime.linkclient.keep import WITHDRAWN
    d = Database(str(tmp_path / "gw.db")); d.init_schema()
    s = GatewayServices(d)
    s.db.set_state(s._KEEP_ASK_KEY, json.dumps({"why": "restore", "hash": "ab" * 8, "items": {}}))
    s.keep_mark_msg(config.ADMIN_ID, 77, "💾 <b>Настройки шлюза из бэкапа</b>\n\nСервер AWG восстановлен")
    edits = []

    async def edit(chat, mid, text, hide=False):
        edits.append((chat, mid, text, hide))
        return True
    monkeypatch.setattr(linkclient, "_edit", edit)
    c = LinkClient(s)
    c._writer = object()

    async def send(kind, body=None, pad=0):
        return True
    c._send = send
    asyncio.run(c._keep_reply({"t": "keep_ok", "hash": "cd" * 8}))
    assert edits == [(config.ADMIN_ID, 77, "<b>Не актуально: восстановление из бэкапа отменено. Текущие настройки "
                      "шлюза сохранены на сервере.</b>\n\n<blockquote expandable>💾 <b>Настройки шлюза из бэкапа</b>"
                      "\n\nСервер AWG восстановлен</blockquote>", True)], edits
    assert s.keep_section() == "" and WITHDRAWN.startswith("Не актуально: ")
    d.close()


def test_a_finisher_fits_the_message_limit():
    """Исходный текст под катом с пометкой сверху — не длиннее 4096 видимых
    символов: не влезает — без разметки и с отрезанным хвостом."""
    import html
    import re
    long = "<b>x</b> " + "строка вопроса " * 400
    out = texts.stale_finisher("Не актуально: прислан новый файл бэкапа", long)
    visible = html.unescape(re.sub(r"<[^>]+>", "", out))
    assert len(visible) <= 4096 and out.endswith("…</blockquote>") and out.startswith("<b>Не актуально")
    nested = texts.stale_finisher("Не актуально", "текст<blockquote expandable>подробнее</blockquote>")
    assert nested.count("<blockquote") == 1, "вложенный кат"
