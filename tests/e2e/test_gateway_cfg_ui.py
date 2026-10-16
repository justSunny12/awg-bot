"""
Вопросы о документе с сервера AWG в чате агента шлюза: строка «⏳ Ждут
решения» и кнопка «⏳ Решить» на панели, экран вопроса, ответы «Оставить
своё» и «Принять с сервера AWG», снятие вопроса ручной правкой, крючок канала.

Здесь человек решает, чей ящик и чья фраза будут на шлюзе. Цена ошибки:
кнопка, которая ничего не делает или делает обратное; ответ, который не
дошёл до сервера (и тот спросит снова на каждой доставке); вопрос о ящике,
который человек уже сменил руками, — «Принять» затёр бы его свежую правку.

Документ применяется настоящим агентом (`apply_cfg`) на подставном хосте
`_Host` из теста документа; ответ серверу (`linkclient.report_cfg_answer`)
подменён записью; вход в ящик — подставной.
"""
from __future__ import annotations

import pytest

import awgbot.core.config as cfg
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers import gateway as gh
from awgbot.bot.texts.gateway.ask import QUESTION_STALE
from awgbot.domain import gwcfg
from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra import mail as mailmod
from awgbot.infra.db import Database
from awgbot.runtime import linkclient
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.unit import test_gw_cfg_apply as _apply
from tests.unit.test_gw_cfg_apply import MAIL, _doc

# фикстуры теста документа: своя копия conf и подставной хост
conf_dir = _apply.conf_dir
host = _apply.host

OWN = {"login": "own@example.net", "password": "DUMMY-own", "imap_host": "imap.example.net",
       "imap_port": 993, "smtp_host": "smtp.example.net", "smtp_port": 587}


@pytest.fixture()
def svc(tmp_path, conf_dir, host, monkeypatch):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    s = GatewayServices(d)
    monkeypatch.setattr(s, "cached_status", lambda max_age: GwStatus(link_up=True, handshake_age=5.0))
    yield s
    d.close()


@pytest.fixture()
def reported(monkeypatch):
    """Что ушло серверу ответами человека."""
    got: list[dict] = []

    async def report(services, answer):
        got.append(answer)
    monkeypatch.setattr(linkclient, "report_cfg_answer", report)
    return got


@pytest.fixture()
def login_ok(monkeypatch):
    monkeypatch.setattr(mailmod, "check_imap", lambda a, timeout=15.0: None)
    monkeypatch.setattr(mailmod, "check_smtp", lambda a, timeout=15.0: None)


def _own_mail(svc):
    svc.email_save(OWN["login"], OWN["password"], OWN["imap_host"], OWN["imap_port"],
                   OWN["smtp_host"], OWN["smtp_port"])


def _nav(fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    return msg, FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


def _sent_texts(fake_bot) -> list[str]:
    return [r[2] for r in fake_bot.records if r[0] == "send_message"]


# ── панель ───────────────────────────────────────────────────────────────────

async def test_the_panel_shows_the_queue_and_the_decide_button(svc, fake_bot):
    """Вопрос висит — на панели строка «⏳ Ждут решения: почта» и кнопка
    «⏳ Решить»; вопросов нет — ни строки, ни кнопки."""
    msg, cb = _nav(fake_bot)
    await gh.gw_panel(cb, svc, FakeState())
    text, markup = msg.sent[-1][1], msg.sent[-1][2]
    assert "Ждут решения" not in text and "⏳ Решить" not in _labels(markup)
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    await gh.gw_panel(cb, svc, FakeState())
    text, markup = msg.sent[-1][1], msg.sent[-1][2]
    assert "⏳ Ждут решения: почта" in text, text
    assert "⏳ Ответить (1)" in _labels(markup), _labels(markup)


async def test_decide_without_questions_says_so_and_shows_the_panel(svc, fake_bot):
    """«⏳ Решить» со старой панели, когда решать уже нечего: всплывашка и
    свежая панель — а не пустой экран вопроса."""
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_ask(cb, svc, FakeState())
    assert cb.answers and "Вопросы уже решены" in cb.answers[-1][0], cb.answers
    kind, text, markup = msg.sent[-1]
    assert kind == "edit_text" and text.startswith("🛰 "), text
    assert "⏳ Решить" not in _labels(markup)


async def test_decide_opens_the_first_question(svc, fake_bot):
    """С вопросом — экран вопроса правкой панели: что отличается и две
    кнопки, ответ привязан к разделу."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_ask(cb, svc, FakeState())
    kind, text, markup = msg.sent[-1]
    assert kind == "edit_text" and "Почта на шлюзе отличается" in text and "Различия: ящик" in text, text
    assert _labels(markup) == ["⬅️ Оставить своё", "✅ Принять", "⬅️ В меню"]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert datas == [GwCB(action="cfg_no", val="mail").pack(), GwCB(action="cfg_ok!", val="mail").pack(),
                     GwCB(action="panel").pack()]
    for secret in (MAIL["login"], MAIL["password"], MAIL["imap_host"], OWN["login"], OWN["password"]):
        assert secret not in text


# ── ответы ───────────────────────────────────────────────────────────────────

async def test_keep_own_answers_the_server_and_opens_the_next_question(svc, fake_bot, reported):
    """«Оставить своё»: итог правкой вопроса, памятка по серверному
    отпечатку, ответ серверу; в очереди есть фраза — она открывается новым
    меню."""
    _own_mail(svc)
    svc.backup_set_passphrase("DUMMY-own-phrase")
    svc.apply_cfg(_doc(mail=MAIL, backup={"phrase": "DUMMY-server-phrase"}))
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_no", val="mail"), svc, FakeState())
    edits = [t for k, t, _ in msg.sent if k == "edit_text"]
    assert edits[-1].startswith("ℹ️ Почта шлюза осталась прежней"), edits
    assert svc.cfg_keep() == {"mail": gwcfg.mail_print(MAIL)}
    assert svc.email_account().login == OWN["login"]
    assert len(reported) == 1 and reported[0]["section"] == "mail" and reported[0]["accepted"] is False
    assert reported[0]["gen"] == 7 and reported[0]["print"] == gwcfg.mail_print(MAIL)
    sent = _sent_texts(fake_bot)
    assert sent and "Ключевая фраза бэкапов на шлюзе отличается от фразы сервера AWG" in sent[-1], sent


async def test_the_last_answer_returns_to_the_panel(svc, fake_bot, reported, login_ok):
    """«Принять с сервера AWG» для почты: почта встала и проверена, итог
    правкой вопроса; очередь пуста — панель, уже без «⏳ Решить»."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="mail"), svc, FakeState())
    edits = [t for k, t, _ in msg.sent if k == "edit_text"]
    assert edits[-1] == "✉️ Почта с сервера AWG принята: IMAP и SMTP отвечают, вход выполнен", edits
    assert svc.email_account().login == MAIL["login"]
    assert reported and reported[0]["accepted"] is True and svc.cfg_questions() == []
    panels = [(t, m) for k, t, m in msg.sent if k == "answer"] + [
        (r[2], None) for r in fake_bot.records if r[0] == "send_message"]
    assert panels and panels[-1][0].startswith("🛰 ") and "Ждут решения" not in panels[-1][0], panels


async def test_accepting_mail_with_a_failed_login_says_why(svc, fake_bot, reported, monkeypatch):
    monkeypatch.setattr(mailmod, "check_imap",
                        lambda a, timeout=15.0: (_ for _ in ()).throw(mailmod.MailError("IMAP отверг пароль")))
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="mail"), svc, FakeState())
    edits = [t for k, t, _ in msg.sent if k == "edit_text"]
    assert "но не работает: IMAP отверг пароль. Проверь логин и пароль" in edits[-1], edits
    assert MAIL["password"] not in edits[-1]


async def test_a_question_overtaken_by_a_manual_change_is_dropped(svc, fake_bot, reported, login_ok):
    """Ящик сменили руками, пока вопрос висел: «Принять» его не затирает —
    вопрос снят без памятки, ответа серверу нет (новый снимок покажет новое
    стоящее, и сервер спросит заново)."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    svc.email_save("hand@example.net", "DUMMY-hand", OWN["imap_host"], OWN["imap_port"],
                   OWN["smtp_host"], OWN["smtp_port"])
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="mail"), svc, FakeState())
    edits = [t for k, t, _ in msg.sent if k == "edit_text"]
    assert edits[-1] == QUESTION_STALE["mail"], edits
    assert svc.email_account().login == "hand@example.net", "ручную правку затёрли почтой сервера"
    assert reported == [] and svc.cfg_keep() == {} and svc.cfg_questions() == []


async def test_a_second_tap_on_an_answered_question_changes_nothing(svc, fake_bot, reported):
    """Двойное нажатие (или кнопка со старого сообщения): второй ответ не
    уходит серверу и памятку не переписывает."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_no", val="mail"), svc, FakeState())
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="mail"), svc, FakeState())
    assert len(reported) == 1, reported
    assert svc.cfg_keep() == {"mail": gwcfg.mail_print(MAIL)} and svc.email_account().login == OWN["login"]
    assert cb.answers and "уже решён" in cb.answers[-1][0], cb.answers


async def test_an_unknown_section_is_refused(svc, fake_bot, reported):
    msg, cb = _nav(fake_bot)
    await gh.gw_cfg_answer(cb, GwCB(action="cfg_ok!", val="token"), svc, FakeState())
    assert reported == [] and cb.answers and "Кнопка устарела" in cb.answers[-1][0]


# ── крючок канала ────────────────────────────────────────────────────────────

async def test_the_channel_hook_opens_the_question_as_a_new_menu(svc, fake_bot):
    """Документ принёс вопрос — крючок канала показывает его новым живым
    меню в чате админа; вопросов нет — молчит."""
    assert await gh.show_cfg_question(fake_bot, svc, cfg.ADMIN_ID) is False
    assert _sent_texts(fake_bot) == []
    svc.db.set_state(svc._BK_KEY_KEY, "RFVNTVlLRVk=")
    svc.apply_cfg(_doc(backup={"phrase": "DUMMY-server-phrase"}))
    assert await gh.show_cfg_question(fake_bot, svc, cfg.ADMIN_ID) is True
    sent = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(sent) == 1 and sent[0][1] == cfg.ADMIN_ID
    assert "шифруются случайным ключом" in sent[0][2], sent[0][2]
    assert "DUMMY-server-phrase" not in sent[0][2]
