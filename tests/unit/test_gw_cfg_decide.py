"""
Почта с сервера AWG на агенте шлюза (domain/gateway/cfg/decide, ask):
правило «молча или вопрос», памятка «оставить своё», снятие вопроса ручной
правкой, тексты вопросов и итогов. Фразы бэкапов и подсказки «куда уходят
бэкапы» больше нет: своих копий у шлюза нет — раздел прежнего сервера не
применяется.

Цена ошибки двусторонняя. Сервер, которому «всегда прав», молча заменил бы
свой ящик шлюза (и аварийные письма ушли бы на чужую почту). Сервер, которого
спрашивают каждый раз, превратил бы каждую доставку в один и тот же вопрос в
чате. Поэтому: заглушка — молча, совпало — ничего, своё — вопрос один раз на
серверное значение; и ни логина, ни пароля, ни хостов в чате.

Хост — `_Host` из теста документа: юнит и конфиг линка стоят, документ
несёт тот же env, так что транзакция обвязки — пустая, и проверяется только
почта. Вход в IMAP и SMTP подменён.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.gateway.ask import (QUESTION_STALE, cfg_answer_text, cfg_pending_line,
                                          cfg_question_text)
from awgbot.core import settings
from awgbot.domain import gwcfg
from awgbot.infra import mail as mailmod
from tests.unit import test_gw_cfg_apply as _apply
from tests.unit.test_gw_cfg_apply import DIGEST, MAIL, _doc

pytestmark = pytest.mark.unit

# фикстуры теста документа: своя копия conf, хост, агент на временной БД
conf_dir = _apply.conf_dir
host = _apply.host
svc = _apply.svc

OWN = {"login": "own@example.net", "password": "DUMMY-own", "imap_host": "imap.example.net",
       "imap_port": 993, "smtp_host": "smtp.example.net", "smtp_port": 587}
PHRASE = "DUMMY-server-phrase"


@pytest.fixture()
def checks(monkeypatch):
    """Вход в ящик: записывает, в какой ящик входили; `fail` — текст отказа."""
    state = {"calls": [], "fail": ""}

    def imap(acc, timeout=15.0):
        state["calls"].append(acc.login)
        if state["fail"]:
            raise mailmod.MailError(state["fail"])
    monkeypatch.setattr(mailmod, "check_imap", imap)
    monkeypatch.setattr(mailmod, "check_smtp", lambda acc, timeout=15.0: None)
    return state


def _own_mail(svc):
    svc.email_save(OWN["login"], OWN["password"], OWN["imap_host"], OWN["imap_port"],
                   OWN["smtp_host"], OWN["smtp_port"])


def _login(svc) -> str:
    acc = svc.email_account()
    return acc.login if acc else ""


# ── заглушка — молча ─────────────────────────────────────────────────────────

def test_mail_over_an_empty_box_is_applied_silently(svc, host, checks):
    """Ящика на шлюзе нет — почта сервера встаёт без вопроса, проверяется
    входом, серверу — applied, человеку — итог проверки."""
    res = svc.apply_cfg(_doc(mail=MAIL))
    assert _login(svc) == MAIL["login"] and svc.email_account().imap_host == MAIL["imap_host"]
    assert checks["calls"] == [MAIL["login"]], "почту сохранили без проверки входа"
    assert "mail" in res["ack"]["applied"] and not res["ack"]["wait"] and res["ask"] == []
    assert "✉️ Почта с сервера AWG принята: IMAP и SMTP отвечают" in res["note"], res["note"]
    assert svc.cfg_wait() == {} and svc.cfg_pending() == {}


def test_a_half_set_box_counts_as_empty(svc, host, checks):
    """Логин есть, пароля нет — это не «своя почта», а недоделанная заглушка:
    вопрос о ней человеку ничего бы не сказал."""
    svc.db.set_state(svc._MAIL_LOGIN_KEY, "half@example.net")
    res = svc.apply_cfg(_doc(mail=MAIL))
    assert _login(svc) == MAIL["login"] and res["ask"] == [] and "mail" in res["ack"]["applied"]


def _with_phrase(doc: dict) -> dict:
    """Документ прежнего сервера: раздел фразы бэкапов сверху."""
    doc["backup"] = {"phrase": PHRASE}
    return doc


def test_a_phrase_section_of_an_older_server_is_not_applied(svc, host):
    """Своих копий у шлюза нет: фраза прежнего сервера не ложится в базу, не
    отвечается и не спрашивается."""
    res = svc.apply_cfg(_with_phrase(_doc()))
    assert not svc.db.get_state("backup_passphrase")
    assert "backup" not in res["ack"]["applied"] and "backup" not in res["ack"]["wait"] and res["ask"] == []
    assert "фраз" not in res["note"]
    assert not hasattr(svc, "backup_set_passphrase") and not hasattr(svc, "cfg_backup_print")


def test_a_failed_login_still_keeps_the_mail_and_says_why(svc, host, checks):
    """Проверка входа не прошла — почта всё равно сохранена (сервер прислал
    её как желаемое), человеку — причина."""
    checks["fail"] = "IMAP отверг логин/пароль"
    res = svc.apply_cfg(_doc(mail=MAIL))
    assert _login(svc) == MAIL["login"]
    assert "но не работает: IMAP отверг логин/пароль. Проверь логин и пароль" in res["note"], res["note"]


def test_a_backup_hint_in_the_mail_section_switches_nothing(svc, host, checks):
    """Подсказка прежнего сервера «бэкапы — на почту»: шлюзу переключать нечего,
    почта применяется как есть, без строки про бэкапы и без отметок."""
    res = svc.apply_cfg(_with_phrase(_doc(mail={**MAIL, "backup": "email"})))
    assert _login(svc) == MAIL["login"] and "mail" in res["ack"]["applied"]
    assert settings.get("app.scheduler.backup_channel", "") != "email"
    assert "💾" not in res["note"] and not svc.db.get_state("gwcfg_mail_hint")


# ── совпало — ничего ─────────────────────────────────────────────────────────

def test_the_same_mail_changes_nothing(svc, host, checks):
    """Стоит то же, что у сервера: ни сохранения, ни входа в ящик, ни
    уведомления — раздел просто «применён»."""
    svc.email_save(MAIL["login"], MAIL["password"], MAIL["imap_host"], MAIL["imap_port"],
                   MAIL["smtp_host"], MAIL["smtp_port"])
    assert svc.cfg_decide("mail", MAIL) == ("same", gwcfg.mail_print(MAIL))
    res = svc.apply_cfg(_doc(mail=MAIL))
    assert checks["calls"] == [] and res["note"] == "" and "mail" in res["ack"]["applied"]


# ── своё — вопрос ────────────────────────────────────────────────────────────

def test_own_mail_waits_for_the_human_and_stays(svc, host, checks):
    """Своя почта, у сервера другая: стоящее не тронуто, серверное — в
    pending, серверу — wait с отпечатком серверного значения, человеку — вопрос."""
    _own_mail(svc)
    res = svc.apply_cfg(_doc(mail=MAIL))
    sp = gwcfg.mail_print(MAIL)
    assert _login(svc) == OWN["login"] and checks["calls"] == [], "своя почта заменена без вопроса"
    assert res["ack"]["wait"] == {"mail": sp} and "mail" not in res["ack"]["applied"]
    assert res["ask"] == ["mail"]
    assert svc.cfg_pending()["mail"]["login"] == MAIL["login"]
    assert svc.cfg_wait()["mail"]["server"] == sp and svc.cfg_wait()["mail"]["gen"] == 7
    assert svc.cfg_questions() == ["mail"]
    assert "почта" not in res["note"], "о вопросе в уведомлении писать нечего — он придёт меню"


def test_the_question_is_asked_once_per_server_value(svc, host, checks):
    """Повтор того же документа, пока вопрос висит, — снова wait, но не
    новый вопрос; новое серверное значение — новый вопрос вместо прежнего."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    again = svc.apply_cfg(_doc(mail=MAIL))
    assert again["ask"] == [] and again["ack"]["wait"] == {"mail": gwcfg.mail_print(MAIL)}
    newer = {**MAIL, "password": "DUMMY-pass-2"}
    res = svc.apply_cfg(_doc(gen=8, mail=newer))
    assert res["ask"] == ["mail"], "новое серверное значение не спросили"
    assert svc.cfg_pending()["mail"]["password"] == "DUMMY-pass-2" and svc.cfg_wait()["mail"]["gen"] == 8


def test_accepting_applies_the_server_mail(svc, host, checks):
    """«Принять с сервера AWG»: почта сервера встаёт и проверяется, памятки
    нет, вопрос снят; ответ — applied от имени спросившего документа."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    ans = svc.cfg_answer("mail", True)
    assert ans["ok"] and ans["accepted"] and ans["note"]["state"] == "ok"
    assert (ans["gen"], ans["hash"], ans["print"]) == (7, DIGEST, gwcfg.mail_print(MAIL))
    assert _login(svc) == MAIL["login"] and checks["calls"] == [MAIL["login"]]
    assert svc.cfg_keep() == {} and svc.cfg_pending() == {} and svc.cfg_wait() == {}
    assert svc.cfg_questions() == []
    assert svc.cfg_answer_ack(ans)["applied"] == ["mail"]


def test_keeping_own_remembers_the_server_value_and_does_not_ask_again(svc, host, checks):
    """«Оставить своё»: своё не тронуто, памятка — отпечаток серверного
    значения; тот же документ снова — declined без вопроса; сервер сменил
    почту — новый вопрос."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    ans = svc.cfg_answer("mail", False)
    sp = gwcfg.mail_print(MAIL)
    assert ans["ok"] and not ans["accepted"] and ans["print"] == sp
    assert _login(svc) == OWN["login"] and checks["calls"] == []
    assert svc.cfg_keep() == {"mail": sp} and svc.cfg_pending() == {} and svc.cfg_wait() == {}
    assert svc.cfg_answer_ack(ans)["declined"] == {"mail": sp}

    res = svc.apply_cfg(_doc(gen=8, mail=MAIL))
    assert res["ack"]["declined"] == {"mail": sp} and res["ask"] == [] and not res["ack"]["wait"], res
    assert svc.cfg_questions() == [], "отказ от того же значения спросили снова"

    res = svc.apply_cfg(_doc(gen=9, mail={**MAIL, "imap_host": "imap2.example.org"}))
    assert res["ask"] == ["mail"], "сервер сменил почту — а вопроса нет"


def test_an_answer_without_a_question_is_not_ok(svc, host):
    assert svc.cfg_answer("mail", True)["ok"] is False
    assert svc.cfg_answer("backup", False)["ok"] is False and svc.cfg_keep() == {}, "раздел прежних версий"


def test_the_question_names_what_differs_without_values(svc, host):
    """«ящик» — другой логин или пароль, «серверы IMAP/SMTP» — другие хосты
    или порты; значений в вопросе нет."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail={**OWN, "password": "DUMMY-other", "backup": ""}))
    assert svc.cfg_question("mail") == {"section": "mail", "differs": ["ящик"]}
    svc.apply_cfg(_doc(gen=8, mail={**OWN, "smtp_port": 465, "backup": ""}))
    assert svc.cfg_question("mail") == {"section": "mail", "differs": ["серверы IMAP/SMTP"]}
    svc.apply_cfg(_doc(gen=9, mail=MAIL))
    assert svc.cfg_question("mail")["differs"] == ["ящик", "серверы IMAP/SMTP"]


def test_the_queue_holds_mail_but_no_phrase(svc, host):
    _own_mail(svc)
    res = svc.apply_cfg(_with_phrase(_doc(mail=MAIL)))
    assert res["ask"] == ["mail"] and svc.cfg_questions() == ["mail"]


# ── ручная правка снимает вопрос ─────────────────────────────────────────────

def test_a_manual_change_while_asked_drops_the_question_without_a_memo(svc, host):
    """Пока вопрос висел, человек сам сменил ящик здесь: вопрос потерял смысл
    и снимается без памятки — следующий документ с той же почтой сервера
    спросит заново по новому стоящему."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    assert not svc.cfg_question_stale("mail")
    svc.email_save("hand@example.net", "DUMMY-hand", OWN["imap_host"], OWN["imap_port"],
                   OWN["smtp_host"], OWN["smtp_port"])
    assert svc.cfg_question_stale("mail")
    assert svc.cfg_questions() == [] and svc.cfg_question("mail") is None
    assert svc.cfg_wait() == {} and svc.cfg_pending() == {} and svc.cfg_keep() == {}
    assert _login(svc) == "hand@example.net"


# ── тексты: ни одного значения в чате ────────────────────────────────────────

_SECRETS = (MAIL["login"], MAIL["password"], MAIL["imap_host"], MAIL["smtp_host"],
            OWN["login"], OWN["password"], OWN["imap_host"], OWN["smtp_host"])


def test_questions_and_answers_carry_no_secret(svc, host, checks):
    """Чат агента — не сейф: логин, пароль и хосты в вопросы и итоги не
    попадают ни при каком ответе."""
    _own_mail(svc)
    svc.apply_cfg(_doc(mail=MAIL))
    shown = [cfg_question_text(svc.cfg_question("mail"))]
    mail_yes = svc.cfg_answer("mail", True)
    shown += [cfg_answer_text("mail", True, mail_yes["note"]), cfg_answer_text("mail", False, {}),
              cfg_answer_text("mail", True, {"state": "fail", "why": "IMAP: таймаут"})]
    for text in shown:
        for secret in _SECRETS:
            assert secret not in text, f"в чат ушло значение {secret!r}: {text}"
    assert "Различия: ящик, серверы IMAP/SMTP" in shown[0]


def test_answer_texts_say_what_happened():
    assert cfg_answer_text("mail", False, None).startswith("ℹ️ Почта шлюза осталась прежней")
    assert "но не работает: таймаут. Проверь логин и пароль" in cfg_answer_text("mail", True, {"state": "fail", "why": "таймаут"})
    assert "SMTP — авторизация отклонена" in cfg_answer_text("mail", True, {"state": "fail", "why": "SMTP: авторизация отклонена"})
    assert "💾" not in cfg_answer_text("mail", True, {"state": "ok", "backup": True}), "бэкапы на почту — у шлюза их нет"
    assert set(QUESTION_STALE) == {"mail", "uplink"}


def test_the_pending_line_lists_the_queue_in_order():
    assert cfg_pending_line(["mail", "keep"]) == "⏳ Ждут решения: почта, настройки из бэкапа"
    assert cfg_pending_line(["uplink", "keep_dev"]) == "⏳ Ждут решения: конфиг аплинка, настройки прежнего устройства"
    assert cfg_pending_line([]) == "", "нечего решать — строки нет"
