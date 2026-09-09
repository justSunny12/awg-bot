"""
mailmix.py — почтовый канал как часть сервисов: аккаунт, сохранение, проверка.

Креды — в БД (server_state): БД и так хранит приватные ключи устройств, лежит
под root с правами 600 и в бэкап уходит только шифрованной; отдельный ключ ради
пароля ящика ничего бы не добавил. Серверы и параметры функций — в
conf/email.yaml через settings (горячо, с комментариями).

Раньше креды жили в /etc/awg-bot/env: их читал только старт, и любая правка
требовала SSH и рестарта. Переезд — email_import_env_once() при старте.
"""
from __future__ import annotations

import logging

from awgbot.core import config, settings
from awgbot.infra import mail
from awgbot.util import timeutil

log = logging.getLogger("awgbot.mail")


class MailMixin:
    _MAIL_LOGIN_KEY = "email_login"
    _MAIL_PASSWORD_KEY = "email_password"
    _MAIL_CHECK_KEY = "email_check"          # "ok|<iso>" / "fail|<iso>|<текст>"

    # ── аккаунт ──────────────────────────────────────────────────────────────

    def email_account(self):
        """MailAccount или None, если чего-то не хватает."""
        login = self.db.get_state(self._MAIL_LOGIN_KEY) or ""
        password = self.db.get_state(self._MAIL_PASSWORD_KEY) or ""
        acc = mail.MailAccount(
            login=login, password=password,
            imap_host=str(settings.get("email.imap_host", "") or ""),
            imap_port=settings.get_int("email.imap_port", 993),
            smtp_host=str(settings.get("email.smtp_host", "") or ""),
            smtp_port=settings.get_int("email.smtp_port", 587))
        return acc if acc.complete() else None

    def email_configured(self) -> bool:
        return self.email_account() is not None

    def email_save(self, login: str, password: str, imap_host: str, imap_port: int,
                   smtp_host: str, smtp_port: int) -> None:
        """Сохранить ящик: креды — в БД, серверы — в email.yaml. Адрес для писем
        от клиентов по умолчанию — сам ящик, если раньше не задан."""
        self.db.set_state(self._MAIL_LOGIN_KEY, login.strip())
        self.db.set_state(self._MAIL_PASSWORD_KEY, password)
        settings.set_value("email.imap_host", imap_host.strip())
        settings.set_value("email.imap_port", int(imap_port))
        settings.set_value("email.smtp_host", smtp_host.strip())
        settings.set_value("email.smtp_port", int(smtp_port))
        if not str(settings.get("email.resume_address", "") or "").strip():
            settings.set_value("email.resume_address", login.strip())
        self.db.set_state(self._MAIL_CHECK_KEY, "")

    def email_forget(self) -> None:
        self.db.set_state(self._MAIL_LOGIN_KEY, "")
        self.db.set_state(self._MAIL_PASSWORD_KEY, "")
        self.db.set_state(self._MAIL_CHECK_KEY, "")
        for key in ("email.imap_host", "email.smtp_host"):
            try:
                settings.set_value(key, "")
            except settings.SettingsWriteError:
                pass

    # ── проверка и тест ──────────────────────────────────────────────────────

    def email_check(self, acc=None) -> tuple[bool, str]:
        """Вход по IMAP и SMTP. (ok, текст). Итог запоминается для экрана."""
        acc = acc or self.email_account()
        if acc is None:
            return False, "ящик не настроен"
        try:
            mail.check_imap(acc)
            mail.check_smtp(acc)
        except mail.MailError as e:
            self.db.set_state(self._MAIL_CHECK_KEY, f"fail|{timeutil.now_iso()}|{e}")
            return False, str(e)
        self.db.set_state(self._MAIL_CHECK_KEY, f"ok|{timeutil.now_iso()}")
        return True, "IMAP и SMTP отвечают, вход выполнен"

    def email_last_check(self) -> tuple[str, str, str]:
        """(ok|fail|"", iso, детали)."""
        raw = self.db.get_state(self._MAIL_CHECK_KEY) or ""
        parts = raw.split("|", 2)
        if len(parts) < 2:
            return "", "", ""
        return parts[0], parts[1], (parts[2] if len(parts) > 2 else "")

    def email_send_test(self) -> None:
        acc = self.email_account()
        if acc is None:
            raise mail.MailError("ящик не настроен")
        mail.send_mail(acc, acc.login, "awg-bot: тестовое письмо",
                       f"Почтовый канал бота работает. {timeutil.now_iso()}")

    # ── функции на канале ────────────────────────────────────────────────────

    def email_resume_enabled(self) -> bool:
        """Аварийный выход из паузы: ящик настроен И рубильник включён."""
        return (self.email_account() is not None
                and settings.get_bool("email.resume_enabled", True))

    def email_resume_address(self) -> str:
        """Куда клиент шлёт код: алиас из conf, иначе сам ящик."""
        alias = str(settings.get("email.resume_address", "") or "").strip()
        return alias or (self.db.get_state(self._MAIL_LOGIN_KEY) or "")

    # ── функции на канале: бэкап и запасной канал для алертов ───────────────

    def backup_channel(self) -> str:
        """telegram | email; email — только при настроенной почте, иначе Telegram."""
        ch = str(settings.get("app.scheduler.backup_channel", "telegram") or "telegram").lower()
        return "email" if ch == "email" and self.email_account() is not None else "telegram"

    def email_send_backup(self, paths) -> None:
        """Файлы бэкапа — вложениями на сам ящик. Только шифрованные: в БД
        приватные ключи, открытыми по почте они не ездят."""
        acc = self.email_account()
        if acc is None:
            raise mail.MailError("ящик не настроен")
        if not self.backup_encryption_enabled():
            raise mail.MailError("бэкап без шифрования по почте не отправляется — "
                                 "задай парольную фразу в 💾 Резервное копирование → 🔐 Шифрование")
        import os
        att = []
        for p in paths:
            with open(p, "rb") as f:
                att.append((os.path.basename(p), f.read()))
        stamp = timeutil.now().strftime("%d.%m.%Y %H:%M")
        mail.send_mail(acc, acc.login, f"awg-bot: резервная копия {stamp}",
                       "Файлы резервной копии во вложении. Расшифровка — restore_backup.py "
                       "с BACKUP_KEY/BACKUP_PASSPHRASE.", attachments=att)

    def email_alert_fallback_enabled(self) -> bool:
        return (settings.get_bool("notifications.email_fallback", False)
                and self.email_account() is not None)

    def email_send_alert(self, text: str) -> None:
        """Критичный алерт админу письмом — когда Telegram не отвечает."""
        acc = self.email_account()
        if acc is None:
            raise mail.MailError("ящик не настроен")
        import re
        plain = re.sub(r"<[^>]+>", "", text)
        mail.send_mail(acc, acc.login, "awg-bot: критичный алерт (Telegram недоступен)",
                       plain + f"\n\n{timeutil.now_iso()}")

    # ── переезд из env ───────────────────────────────────────────────────────

    def email_import_env_once(self) -> bool:
        """Креды из /etc/awg-bot/env (прежняя схема) → БД, один раз: только если
        в БД пусто. Возвращает True, если перенесли."""
        if self.db.get_state(self._MAIL_LOGIN_KEY):
            return False
        login, password = config.EMAIL_RESUME_LOGIN, config.EMAIL_RESUME_PASSWORD
        if not (login and password):
            return False
        self.db.set_state(self._MAIL_LOGIN_KEY, login)
        self.db.set_state(self._MAIL_PASSWORD_KEY, password)
        log.info("почта: креды перенесены из env в БД")
        return True

    @staticmethod
    def email_env_leftover() -> bool:
        """В env всё ещё лежат EMAIL_RESUME_* — напомнить убрать."""
        return bool(config.EMAIL_RESUME_LOGIN or config.EMAIL_RESUME_PASSWORD)
