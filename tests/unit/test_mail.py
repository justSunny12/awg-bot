"""Почтовый канал: провайдеры, адреса, хранение кредов в БД, переезд из env."""
from __future__ import annotations

import pytest

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


def test_env_credentials_migrate_once(services, monkeypatch):
    monkeypatch.setattr(cfg, "EMAIL_RESUME_LOGIN", "old@icloud.com")
    monkeypatch.setattr(cfg, "EMAIL_RESUME_PASSWORD", "oldpw")
    assert services.email_import_env_once() is True
    assert services.db.get_state("email_login") == "old@icloud.com"
    assert services.email_import_env_once() is False           # уже есть — не трогаем
    assert services.email_env_leftover() is True


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


def test_mail_settings_travel_in_the_bundle(services, monkeypatch, tmp_path):
    """ВПС кладёт почту в бандл одной строкой, агент принимает и сохраняет."""
    from awgbot.core import settings
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v))
    monkeypatch.setattr(settings, "get", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(settings, "get_int", lambda k, d=0: int(store.get(k, d)))
    monkeypatch.setattr(settings, "get_bool", lambda k, d=True: bool(store.get(k, d)))
    plain = b"#!/bin/sh\nSERVER_NAME=\"awg-srv\"\n#__GW_SETUP_BELOW__\nrest\n"
    assert services._bundle_with_mail(plain) == plain, "без почты бандл не меняется"
    services.email_save("box@icloud.com", "p@ss\"word", "imap.mail.me.com", 993, "smtp.mail.me.com", 587)
    out = services._bundle_with_mail(plain)
    assert b'MAIL_B64="' in out and out.index(b"MAIL_B64") < out.index(b"#__GW_SETUP_BELOW__")
    assert b"p@ss" not in out, "пароль в бандле не открытым текстом"
    gdb = Database(tmp_path / "gw.db"); gdb.init_schema()
    gw = GatewayServices(gdb)
    assert gw._apply_bundle_mail(out.decode()) is True
    acc = gw.email_account()
    assert acc and acc.login == "box@icloud.com" and acc.password == "p@ss\"word" and acc.smtp_host == "smtp.mail.me.com"


def test_backup_mailed_plural():
    from awgbot.bot import texts
    assert texts.backup_mailed("a@b.co", 1).startswith("📨 Резервная копия (1 файл) отправлена")
    assert "(2 файла)" in texts.backup_mailed("a@b.co", 2)
    assert "(5 файлов)" in texts.backup_mailed("a@b.co", 5) and not texts.backup_mailed("a@b.co", 5).endswith(".")
