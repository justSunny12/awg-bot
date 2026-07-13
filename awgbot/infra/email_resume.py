"""email_resume.py — аварийный email-выход из приостановки.

Клиент, заперевшийся в паузе (Telegram доступен только через этот VPN), присылает
одноразовый resume-код письмом на алиас-ящик. Бот ИСХОДЯЩЕ опрашивает IMAP
(портов на хосте не открываем), находит письмо с валидным кодом в теме, снимает
паузу соответствующему клиенту и отвечает письмом об успехе.

Безопасность:
- From письма НЕ доверяем (спуфится) — единственный секрет — код в теме, он
  известен только тому, кто входил в паузу; одноразовый, живёт пока активна пауза.
- тело письма НЕ читаем вообще (дешевле и меньше поверхность) — только Subject.
- невалидный/неизвестный код → молча помечаем письмо \\Seen, ответа нет.
- код из безопасного алфавита без похожих символов (0/O, 1/l).

Модуль — тонкий слой над imaplib/smtplib (стдлиб, без внешних зависимостей).
Матч кода и снятие паузы делает вызывающий (poller в runtime), здесь — только
IMAP/SMTP-механика и генерация кода.
"""
from __future__ import annotations

import email
import imaplib
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.header import decode_header
from typing import Callable

from awgbot.core import config

# Безопасный алфавит: без 0/O, 1/l/I — чтобы код нельзя было перепутать при
# ручном наборе с телефона.
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz"


def generate_code(length: int = None) -> str:
    """Одноразовый resume-код из безопасного алфавита."""
    n = length or config.EMAIL_RESUME_CODE_LEN
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def _decode_subject(raw: str) -> str:
    """Subject → строка (учитывая MIME-кодирование заголовка)."""
    if not raw:
        return ""
    parts = []
    for chunk, enc in decode_header(raw):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


# Папки, которые проверяем на непрочитанные письма с кодом. Провайдер
# (iCloud/Gmail) мог отправить письмо в «спам» — его тоже смотрим, иначе клиент
# заперся бы из-за спам-фильтра. Секрет — только код в теме, так что читать
# спам-папку не опаснее входящих. Несуществующие папки молча пропускаются.
_MAILBOXES = ("INBOX", "Junk", "Spam", "Junk E-mail", "INBOX.Junk",
              "INBOX.Spam", "[Gmail]/Spam")


def poll_once(on_code: Callable[[str], bool]) -> int:
    """Один цикл опроса ящика. Для каждого НЕпрочитанного письма извлекает код
    из темы и передаёт в on_code(code)->bool: True = код принят (пауза снята),
    False = нет. Письмо в любом случае помечается \\Seen (обработано). При
    принятом коде вызывающий уже снял паузу; ответ об успехе шлём отсюда, только
    если on_code вернул True и есть обратный адрес.

    Смотрим INBOX и известные папки «спам»/«Junk» (несуществующие — пропускаем):
    письмо с кодом могло попасть под спам-фильтр провайдера.

    Возвращает число принятых кодов. Сетевые/протокольные ошибки пробрасывает —
    их гасит и логирует poller (чтобы один сбой не ронял фоновую задачу).
    """
    if not config.EMAIL_RESUME_ENABLED:
        return 0
    accepted = 0
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(config.EMAIL_IMAP_HOST, config.EMAIL_IMAP_PORT,
                             ssl_context=ctx)
    try:
        conn.login(config.EMAIL_RESUME_LOGIN, config.EMAIL_RESUME_PASSWORD)
        for mbox in _MAILBOXES:
            try:
                typ, _ = conn.select(mbox)
            except imaplib.IMAP4.error:
                continue                              # папки нет — пропускаем
            if typ != "OK":
                continue
            accepted += _scan_selected(conn, on_code)
    finally:
        try:
            conn.logout()
        except Exception:                             # noqa: BLE001
            pass
    return accepted


def _scan_selected(conn, on_code: Callable[[str], bool]) -> int:
    """Обработать все непрочитанные письма в УЖЕ выбранной папке."""
    accepted = 0
    typ, data = conn.search(None, "UNSEEN")
    if typ != "OK" or not data or not data[0]:
        return 0
    for num in data[0].split():
        # BODY.PEEK[HEADER] — не читаем тело, только заголовки, и НЕ ставим
        # \\Seen этим фетчем (PEEK); отметку ставим явно ниже.
        typ, msg_data = conn.fetch(num, "(BODY.PEEK[HEADER])")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        hdr = email.message_from_bytes(msg_data[0][1])
        subject = _decode_subject(hdr.get("Subject", ""))
        sender = email.utils.parseaddr(hdr.get("From", ""))[1]
        code = subject.strip()
        ok = False
        if code:
            try:
                ok = bool(on_code(code))
            except Exception:                         # noqa: BLE001
                ok = False
        conn.store(num, "+FLAGS", "\\Seen")           # обработано в любом случае
        if ok:
            accepted += 1
            if sender:
                try:
                    send_success_reply(sender)
                except Exception:                     # noqa: BLE001
                    pass                              # ответ — не критично
    return accepted


def send_success_reply(to_addr: str) -> None:
    """Ответ об успехе (только при принятом коде). Невалидным — не отвечаем."""
    if not config.EMAIL_RESUME_ENABLED or not to_addr:
        return
    msg = EmailMessage()
    msg["From"] = config.EMAIL_RESUME_LOGIN
    msg["To"] = to_addr
    msg["Subject"] = "Доступ восстановлен"
    msg.set_content("Код принят, доступ восстановлен.")
    ctx = ssl.create_default_context()
    with smtplib.SMTP(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(config.EMAIL_RESUME_LOGIN, config.EMAIL_RESUME_PASSWORD)
        s.send_message(msg)
