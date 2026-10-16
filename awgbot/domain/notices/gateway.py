"""gateway.py — часть пакета notices (см. __init__): причина отказа почты
агента шлюза и следующий шаг — общие с экранами агента."""

from __future__ import annotations

import re

from awgbot.util.textfmt import _e


def mail_why(why: str) -> str:
    """Причина отказа почты для человека: «IMAP imap.example.org:993: вход
    отклонён» → «IMAP <code>imap.example.org:993</code> — вход отклонён»,
    «SMTP: авторизация отклонена» → «SMTP — авторизация отклонена». Готовый HTML."""
    why = str(why or "ошибка").strip()
    m = re.match(r"^(IMAP|SMTP)\s+(\S+):\s*(.+)$", why)
    if m:
        return f"{m.group(1)} <code>{_e(m.group(2))}</code> — {_e(m.group(3))}"
    m = re.match(r"^(IMAP|SMTP):\s*(.+)$", why)
    if m:
        return f"{m.group(1)} — {_e(m.group(2))}"
    return _e(why)


MAIL_FIX_TAIL = "Проверь логин и пароль в «✉️ E-mail» в боте сервера AWG"
