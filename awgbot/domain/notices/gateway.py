"""gateway.py — часть пакета notices (см. __init__): отчёт агента шлюза о
применении конфигурации — из статуса скрипта (его же показывает экран итога
применения)."""

from __future__ import annotations

import re

from awgbot.util.textfmt import _e, plural_ru


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


MAIL_FIX_TAIL = "Проверь логин и пароль в «✉️ E-mail» на сервере AWG"

# По этой подстроке экран итога узнаёт частичное применение: «⚠️ Конфигурация
# шлюза применена не полностью» над строкой «VPN-транзит: не применён — …».
APPLY_PARTIAL_MARK = "не применён"

# Причины отказа VPN-транзита из статуса скрипта — со следующим шагом
_LAN_ERROR_HINTS = {
    "dnsmasq не установлен": "поставь пакет dnsmasq и примени конфигурацию ещё раз",
}


def _lan_error(err: str) -> str:
    for key, hint in _LAN_ERROR_HINTS.items():
        if err.startswith(key):
            return f"{_e(err)}: {hint}"
    return _e(err)


def gateway_apply_report(st: dict) -> str:
    """Человеческий отчёт применения конфигурации — из статуса скрипта: аплинк,
    линк и подтверждение одной строкой через « · », фильтр SSH и VPN-транзит —
    строками ниже с прописной; интерфейс и адрес моноширинным. Готовый HTML."""
    head = []
    up = st.get("UPLINK", "")
    if up == "installed":
        head.append("аплинк обновлён")
    elif up == "unchanged":
        head.append("аплинк без изменений")
    link = st.get("LINK", "")
    if link == "up":
        head.append("линк поднят")
    elif link == "foreign":
        head.append("линк лежит: шлюз указывает на устройство, отличное от выбранного")
    elif link == "unconfirmed":
        head.append("линк не тронут: аплинк этого устройства не найден")
    gs = st.get("GW_STATUS", "")
    if gs == "confirmed":
        head.append("шлюз подтверждён")
    elif gs == "unmarked":
        head.append("шлюз в боте сервера AWG не назначен")
    elif gs == "unconfirmed":
        head.append("шлюз не подтверждён")
    rest = []
    if st.get("SSH_FILTER") == "1":
        n = int(st.get("SSH_ALLOW_COUNT") or 0)
        rest.append("Фильтр SSH снаружи: включён, " + (f"{n} " + plural_ru(n, "адрес", "адреса", "адресов")
                                                       if n else "только сервер AWG"))
    elif st.get("SSH_FILTER") == "0":
        rest.append("Фильтр SSH снаружи: выключен")
    if st.get("LAN") == "1":
        err = st.get("LAN_ERROR") or ""
        if err:
            rest.append(f"VPN-транзит: {APPLY_PARTIAL_MARK} — {_lan_error(err)}")
        else:
            rest.append("VPN-транзит: применён"
                        + (f" (<code>{_e(st.get('LAN_IF'))}</code>, <code>{_e(st.get('LAN_ADDR'))}</code>)"
                           if st.get("LAN_IF") else ""))
    lines = ([" · ".join(head)] if head else []) + rest
    if not lines:
        return ""
    text = "\n".join(lines)
    return text[0].upper() + text[1:]
