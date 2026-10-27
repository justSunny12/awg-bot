"""page.py — часть пакета lists (см. __init__): страницы экрана «весь список».

Строки целиком, жадно: страница — пока видимый текст (шапка, строки и
разделители без тегов, с раскрытыми сущностями) не длиннее 3500 единиц UTF-16
и строк не больше 30. Запас до 4096 — на расхождение счёта (эмодзи вне BMP —
две единицы) и на будущие строки шапки; 30 строк держат и число сущностей
разметки в сообщении в разумных пределах. Строка длиннее бюджета (по
построению быть не должно) обрезается «…» — защитно."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Callable

BUDGET = 3500
MAX_ROWS = 30
# сущности разметки (<code>, ссылки, <b>…) на страницу: предела Bot API не
# документирует — держим тот, что проверен на «📶 Онлайн» (30 строк по 3)
MAX_ENTITIES = 90
_TAGS = re.compile(r"<[^>]+>")
_OPEN = re.compile(r"<(?:a\s|code>|b>|i>|u>|s>|pre>|blockquote>)")


def entities(text: str) -> int:
    """Сколько сущностей разметки откроет текст."""
    return len(_OPEN.findall(text))


def visible(text: str) -> int:
    """Длина видимого текста в единицах UTF-16 (как считает Telegram)."""
    plain = html.unescape(_TAGS.sub("", text))
    return len(plain.encode("utf-16-le")) // 2


def _clip(row: str, room: int) -> str:
    """Защитно: строка длиннее страницы — видимым текстом до room и «…» (теги сняты)."""
    plain = html.unescape(_TAGS.sub("", row))
    out, used = [], 0
    for ch in plain:
        w = len(ch.encode("utf-16-le")) // 2
        if used + w > room - 1:
            break
        out.append(ch)
        used += w
    return html.escape("".join(out), quote=False) + "…"


def split(rows: list[str], head_len: int, sep: str = "\n", max_rows: int = MAX_ROWS,
          head_ents: int = 0) -> list[list[str]]:
    """Страницы строк: head_len — видимая длина шапки с её отступом (и хвоста экрана),
    head_ents — сущности разметки шапки и хвоста (входят в предел страницы)."""
    pages: list[list[str]] = []
    cur: list[str] = []
    used, ents = head_len, head_ents
    room = BUDGET - head_len
    gap = visible(sep)
    for row in rows:
        if visible(row) > room or entities(row) > MAX_ENTITIES:
            row = _clip(row, room)
        w = visible(row) + (gap if cur else 0)
        e = entities(row)
        if cur and (used + w > BUDGET or len(cur) >= max_rows or ents + e > MAX_ENTITIES):
            pages.append(cur)
            cur, used, ents = [], head_len, head_ents
            w = visible(row)
        cur.append(row)
        used += w
        ents += e
    if cur or not pages:
        pages.append(cur)
    return pages


@dataclass(frozen=True)
class Page:
    """Страница экрана-списка: текст, номер (после зажима), всего страниц и
    срез строк [start, stop) — для кнопок той же страницы."""
    text: str
    cur: int
    total: int
    start: int
    stop: int

    @property
    def has_prev(self) -> bool:
        return self.cur > 0

    @property
    def has_next(self) -> bool:
        return self.cur < self.total - 1


def label(cur: int, total: int) -> str:
    """«стр. 2 из 3» в шапке — только когда страниц больше одной."""
    return f"стр. {cur + 1} из {total}" if total > 1 else ""


def paged(rows: list[str], page: int, *, header: Callable[[str], str], sep: str = "\n", gap: str = "\n\n",
          tail: str = "", max_rows: int = MAX_ROWS) -> Page:
    """Экран, который сам список, — страницей на месте: header(метка страницы)
    → шапка, строки страницы через sep, хвост экрана (строка «обновлено …»).
    Номер вне диапазона (список сократился между нажатиями) — к последней."""
    probe = header(label(98, 99))
    chunks = split(list(rows), visible(probe) + visible(gap) + visible(tail), sep, max_rows,
                   entities(probe) + entities(tail))
    total = len(chunks)
    cur = min(max(int(page or 0), 0), total - 1)
    start = sum(len(c) for c in chunks[:cur])
    body = sep.join(chunks[cur])
    text = header(label(cur, total)) + (gap + body if body else "") + tail
    return Page(text=text, cur=cur, total=total, start=start, stop=start + len(chunks[cur]))
