"""scan.py — часть пакета lists/msgs: чистые разборы отправленного HTML и
инлайн-клавиатуры (без базы и без Telegram).

Ссылка на снимок в тексте — `<a href="https://t.me/<бот>?start=list-snap-<ключ>[-n]">подпись</a>`
(тот же образец, что у bot/links и util/listlink). Снятие ссылки — подпись
остаётся текстом: видимое не меняется, HTML лишь короче. Клавиатура — JSON
InlineKeyboardMarkup, как хранит запись; «Скрыть» — колбэк `hd`."""
from __future__ import annotations

import json
import re

from aiogram.types import InlineKeyboardMarkup

from awgbot.infra.db.listsnap import kb_has_action, kb_has_hide

_LINK = re.compile(r'<a href="https://t\.me/[A-Za-z0-9_]+\?start=list-snap-(?P<key>[a-z0-9]{10})(?:-n)?">'
                   r'(?P<label>.*?)</a>', re.S)
HIDE_CB = "hd"


def keys(html: str) -> list[str]:
    """Ключи снимков, на которые ссылается текст, по порядку, без повторов."""
    out: list[str] = []
    for m in _LINK.finditer(html or ""):
        k = m.group("key")
        if k not in out:
            out.append(k)
    return out


def strip(html: str, dead) -> str:
    """Ссылки на снимки dead — подписью (текстом); прочие ссылки целы. Повтор
    ничего не меняет."""
    dead = set(dead or ())
    if not dead or not html:
        return html or ""
    return _LINK.sub(lambda m: m.group("label") if m.group("key") in dead else m.group(0), html)


def kb_json(markup) -> str:
    """Клавиатура метода — JSON для записи; не инлайн (reply-клавиатура, снятие) — пусто."""
    if isinstance(markup, InlineKeyboardMarkup) and markup.inline_keyboard:
        return markup.model_dump_json(exclude_none=True)
    return ""


def markup_from(kb: str | None) -> InlineKeyboardMarkup | None:
    """Клавиатура записи → объект для правки; пусто — None (кнопки снимаются)."""
    if not kb:
        return None
    try:
        data = json.loads(kb)
    except ValueError:
        return None
    rows = data.get("inline_keyboard") if isinstance(data, dict) else None
    if not rows:
        return None
    return InlineKeyboardMarkup.model_validate(data)


def without_hide(kb: str | None) -> str:
    """Клавиатура без ряда «Скрыть» (JSON); одиночная «Скрыть» — пусто (кнопок нет).
    Ряд, где «Скрыть» стоит рядом с другой кнопкой, теряет только её."""
    if not kb:
        return ""
    try:
        data = json.loads(kb)
    except ValueError:
        return ""
    rows = data.get("inline_keyboard") if isinstance(data, dict) else None
    kept = []
    for row in rows or []:
        if not isinstance(row, list):
            continue
        left = [b for b in row if isinstance(b, dict) and str(b.get("callback_data") or "") != HIDE_CB]
        if left:
            kept.append(left)
    if not kept:
        return ""
    return json.dumps({"inline_keyboard": kept}, ensure_ascii=False)


def has_hide(kb: str | None) -> bool:
    return kb_has_hide(kb)


def has_action(kb: str | None) -> bool:
    return kb_has_action(kb)
