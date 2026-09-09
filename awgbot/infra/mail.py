"""
mail.py — почтовый канал бота: аккаунт, провайдеры, проверки, отправка.

Тонкий слой над imaplib/smtplib (стдлиб). Аккаунт приходит извне (креды — в БД,
серверы — в conf/email.yaml): модуль ничего не читает сам, поэтому им одинаково
пользуются основной бот и агент шлюза.

Портов на хосте не открывается: бот только сам исходит на почтовый сервер.
"""
from __future__ import annotations

import imaplib
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage


class MailError(Exception):
    """Ошибка почтового канала — текст пригоден для показа админу."""


@dataclass(frozen=True)
class MailAccount:
    login: str
    password: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587

    def complete(self) -> bool:
        return bool(self.login and self.password and self.imap_host and self.smtp_host)


# Провайдеры с известными серверами: адрес → (imap, imap_port, smtp, smtp_port).
# Остальным серверы вводятся руками.
PROVIDERS: dict[str, tuple[str, int, str, int]] = {
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 587),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587),
    "yandex.ru": ("imap.yandex.ru", 993, "smtp.yandex.ru", 587),
    "mail.ru": ("imap.mail.ru", 993, "smtp.mail.ru", 587),
}

# Подсказка про пароль: у этих провайдеров обычный пароль для IMAP не работает.
PASSWORD_HINTS: dict[str, str] = {
    "gmail.com": "нужен «пароль приложения» (Google Account → Security → App passwords), "
                 "обычный пароль Gmail для IMAP не подходит",
    "icloud.com": "нужен app-specific password (account.apple.com → App-Specific Passwords)",
    "yandex.ru": "нужен «пароль приложения» (id.yandex.ru → Безопасность → Пароли приложений) "
                 "и включённый IMAP в настройках Почты",
    "mail.ru": "нужен «пароль для внешних приложений» (id.mail.ru → Безопасность)",
}


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""


def detect_provider(address: str):
    """(imap, imap_port, smtp, smtp_port) для известного домена или None."""
    return PROVIDERS.get(domain_of(address))


def is_address(text: str) -> bool:
    t = (text or "").strip()
    return "@" in t and "." in t.rsplit("@", 1)[-1] and " " not in t and len(t) <= 254


def _ctx():
    return ssl.create_default_context()


def check_imap(acc: MailAccount, timeout: float = 15.0) -> None:
    """Вход по IMAP. Ошибка — MailError с понятным текстом."""
    try:
        conn = imaplib.IMAP4_SSL(acc.imap_host, acc.imap_port, ssl_context=_ctx(), timeout=timeout)
    except OSError as e:
        raise MailError(f"IMAP {acc.imap_host}:{acc.imap_port} недоступен: {e}") from e
    try:
        conn.login(acc.login, acc.password)
    except imaplib.IMAP4.error as e:
        raise MailError(f"IMAP отверг логин/пароль: {_clean(e)}") from e
    finally:
        try:
            conn.logout()
        except Exception:                             # noqa: BLE001
            pass


def check_smtp(acc: MailAccount, timeout: float = 15.0) -> None:
    """Вход по SMTP (STARTTLS), без отправки."""
    try:
        with smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=timeout) as s:
            s.starttls(context=_ctx())
            s.login(acc.login, acc.password)
    except smtplib.SMTPAuthenticationError as e:
        raise MailError(f"SMTP отверг логин/пароль: {_clean(e)}") from e
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"SMTP {acc.smtp_host}:{acc.smtp_port} недоступен: {_clean(e)}") from e


def send_mail(acc: MailAccount, to_addr: str, subject: str, body: str,
              timeout: float = 20.0) -> None:
    msg = EmailMessage()
    msg["From"] = acc.login
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(acc.smtp_host, acc.smtp_port, timeout=timeout) as s:
            s.starttls(context=_ctx())
            s.login(acc.login, acc.password)
            s.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise MailError(f"письмо не ушло: {_clean(e)}") from e


def _clean(e: Exception) -> str:
    s = str(e)
    if isinstance(e, tuple) or s.startswith("(") and "b'" in s:
        s = s.replace("b'", "'")
    return s[:200]
