"""
links.py — грамматика ссылок на экраны (параметр /start) в одном месте.

Разборщики ссылок админа (шапка главной, уведомления) и пользователя
(подписка, РФ-доступ, устройство) — чистые функции над константами
bot/texts; обработчики /start и уведомитель берут их отсюда (уведомитель не
может импортировать обработчики — они импортируют его).

Метка «-n» — последней, у ссылок из уведомлений и следов событий: экран
открывается новым сообщением внизу чата, а не на месте живого меню где-то
выше («ничего не произошло»). Её ставит единственная точка отправки
уведомлений (`notifier._send` → `mark_new`); у ссылок экранов и итогов
метки нет никогда. Коды приглашений метку не получают и не теряют: у
пользователя код проверяется по исходному параметру.
"""
from __future__ import annotations

import re

from awgbot.bot import texts
from awgbot.util import listlink

NEW = "-n"


def _payload_id(payload: str, head: str) -> int | None:
    """«head-<id>» или «head-<id>-t» → id."""
    if not payload.startswith(head + "-"):
        return None
    rest = payload[len(head) + 1:]
    if rest.endswith("-t"):
        rest = rest[:-2]
    return int(rest) if rest.isdigit() else None


def parse_admin(payload: str) -> tuple[str, int] | None:
    """Ссылка шапки и уведомлений админа → (вид экрана реестра, ref) или None."""
    if payload == texts.ONLINE_PAYLOAD:
        return "online", 0
    if payload == texts.EXPIRING_PAYLOAD:
        return "expiring", 0
    if payload == texts.UNASSIGNED_PAYLOAD:
        return "unassigned", 0
    if payload == texts.UPD_PAYLOAD:
        return "upd", 0
    if payload in (texts.TRAFFIC_PAYLOAD, texts.RF_PAYLOAD):
        return "traffic", 0
    if payload == texts.MIGRATION_PAYLOAD:
        return "migration", 0
    if payload == texts.RSV_PAYLOAD:
        return "set_rsv", 0
    for head, kind in ((texts.TRAFFIC_PAYLOAD, "traffic_dev"), (texts.RF_PAYLOAD, "traffic_dev"),
                       ("extend", "extend"), (texts.GW_CARD_PAYLOAD, "gw"), ("cl", "cl"), ("dev", "dev"),
                       (texts.MIGRATION_PAYLOAD, "migration_cl")):
        ref = _payload_id(payload, head)
        if ref is not None:
            return kind, ref
    return None


def parse_client(payload: str) -> tuple[str, int] | None:
    """«sub», «rf», «dev-<id>» → (вид экрана, ref); иначе None (код)."""
    if payload == texts.SUB_PAYLOAD:
        return "sub", 0
    if payload == texts.RF_PAYLOAD_CLIENT:
        return "rf", 0
    head = texts.DEV_PAYLOAD + "-"
    if payload.startswith(head) and payload[len(head):].isdigit():
        return "dev", int(payload[len(head):])
    return None


def is_screen(payload: str) -> bool:
    """Ссылка на экран или экран-список любой роли (без метки «-n»)."""
    return parse_admin(payload) is not None or parse_client(payload) is not None or listlink.is_list(payload)


def strip_new(payload: str) -> tuple[str, bool]:
    """(ссылка без «-n», была ли метка); «-n» снимается, только если без неё
    остаётся ссылка на экран — код приглашения, оканчивающийся на «-n», цел."""
    if payload.endswith(NEW) and is_screen(payload[:-len(NEW)]):
        return payload[:-len(NEW)], True
    return payload, False


_HREF = re.compile(r'(href="https://t\.me/[A-Za-z0-9_]+\?start=)([A-Za-z0-9_-]{1,64})(")')


def _mark(m: re.Match) -> str:
    p = m.group(2)
    if p.endswith(NEW) or not is_screen(p) or len(p) + len(NEW) > 64:
        return m.group(0)
    return m.group(1) + p + NEW + m.group(3)


def mark_new(text: str) -> str:
    """Каждой ссылке на экран или список в тексте уведомления — метка «-n» (один
    раз: повторный проход ничего не меняет). Прочие ссылки и голые адреса — как есть."""
    if not text or "?start=" not in text:
        return text
    return _HREF.sub(_mark, text)
