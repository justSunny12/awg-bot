"""Почтовый канал: провайдеры, адреса, хранение кредов в БД, переезд из env."""
from __future__ import annotations


import awgbot.core.config as cfg
from awgbot.infra import mail


def test_provider_detection_and_address_check():
    assert mail.detect_provider("a@gmail.com") == ("imap.gmail.com", 993, "smtp.gmail.com", 587)
    assert mail.detect_provider("A@ICLOUD.COM")[0] == "imap.mail.me.com"
    assert mail.detect_provider("a@yandex.ru")[2] == "smtp.yandex.ru"
    assert mail.detect_provider("a@mail.ru") is None, "mail.ru — только руками: IMAP по подписке"
    assert mail.detect_provider("a@example.org") is None
    assert mail.is_address("box@example.com") and not mail.is_address("box@") and not mail.is_address("нет")


def test_account_saved_in_db_and_settings(services, monkeypatch):
    from awgbot.core import settings
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: bool(store.get(k, d)))
    assert services.email_account() is None and not services.email_resume_enabled()
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    acc = services.email_account()
    assert acc and acc.login == "box@icloud.com" and acc.password == "pw" and acc.smtp_port == 587
    assert services.db.get_state("email_password") == "pw", "пароль — в БД"
    assert store["email.resume_address"] == "box@icloud.com", "адрес для писем по умолчанию — сам ящик"
    assert services.email_resume_enabled() and services.email_resume_address() == "box@icloud.com"
    store["email.resume_enabled"] = False
    assert not services.email_resume_enabled()
    services.email_forget()
    assert services.email_account() is None and services.db.get_state("email_login") == ""


def test_email_check_records_result(services, monkeypatch):
    acc = mail.MailAccount("a@b.co", "p", "imap.b.co", 993, "smtp.b.co", 587)
    monkeypatch.setattr(mail, "check_imap", lambda a, timeout=15.0: None)
    monkeypatch.setattr(mail, "check_smtp", lambda a, timeout=15.0: (_ for _ in ()).throw(mail.MailError("SMTP отверг логин/пароль")))
    ok, detail = services.email_check(acc)
    assert not ok and "SMTP" in detail and services.email_last_check()[0] == "fail"
    monkeypatch.setattr(mail, "check_smtp", lambda a, timeout=15.0: None)
    ok, _ = services.email_check(acc)
    assert ok and services.email_last_check()[0] == "ok"


def test_password_hints_cover_all_known_providers():
    for dom in mail.PROVIDERS:
        assert dom in mail.PASSWORD_HINTS
        assert "парол" in mail.PASSWORD_HINTS[dom] or "password" in mail.PASSWORD_HINTS[dom].lower()
    assert "Яндекс 360" in mail.PASSWORD_HINTS["yandex.ru"]


def test_backup_mailed_text_and_subject(services, monkeypatch):
    from awgbot.bot import texts
    from awgbot.util import timeutil
    assert texts.backup_mailed("a@b.co") == "📨 Бэкап отправлен на <code>a@b.co</code>"
    sent = []
    monkeypatch.setattr(mail, "send_mail", lambda acc, to, subject, body, **kw: sent.append(subject))
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    services.backup_set_passphrase("correct horse battery")
    monkeypatch.setattr(cfg, "ROLE", "gateway")
    services.email_send_backup([])
    monkeypatch.setattr(cfg, "ROLE", "client")
    services.email_send_backup([])
    yy = timeutil.now().strftime("%d.%m.%y")
    assert sent[0].startswith("awg-bot-gw: резервная копия ") and sent[1].startswith("awg-bot-main: резервная копия ")
    assert f"копия {yy} " in sent[0] and sent[0].endswith(" (UTC+5)"), "год — всегда (письмо лежит годами), время с меткой"


def test_test_and_alert_mail_bodies_show_the_display_zone(services, monkeypatch):
    """Тело тестового и аварийного письма — время пояса показа с меткой и годом,
    не ISO хранения «+03:00»."""
    bodies = []
    monkeypatch.setattr(mail, "send_mail", lambda acc, to, subject, body, **kw: bodies.append(body))
    services.email_save("box@icloud.com", "pw", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    services.email_send_test()
    services.email_send_alert("<b>линк</b> лежит")
    assert all(b.endswith(" (UTC+5)") and "+03:00" not in b for b in bodies), bodies


def test_failed_test_mail_is_recorded_as_the_last_check(services, monkeypatch):
    """Отказ тест-письма — тоже итог проверки: шапка раздела не должна
    остаться «ещё не проверялось» сразу под показанным отказом."""
    import pytest
    acc = mail.MailAccount("a@b.co", "p", "imap.b.co", 993, "smtp.b.co", 587)
    monkeypatch.setattr(services, "email_account", lambda: acc)
    monkeypatch.setattr(mail, "send_mail", lambda *a, **kw: (_ for _ in ()).throw(mail.MailError("SMTP: авторизация отклонена")))
    with pytest.raises(mail.MailError):
        services.email_send_test()
    state, _when, detail = services.email_last_check()
    assert state == "fail" and detail == "SMTP: авторизация отклонена"
