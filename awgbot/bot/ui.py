"""ui.py — сборщик экрана и атомы, общие для текста и клавиатур обеих ролей.

Правила оформления живут здесь один раз, экраны их не повторяют:
  • заголовок — значок и жирное имя: «🔔 <b>Уведомления</b>»; значок вне <b>;
  • статус — через пробел вплотную к одной сущности («🔀 <b>VPN-транзит</b> 🟢 работает»),
    несколько сущностей — через « · » (meta);
  • всплывашка — без тегов и сущностей, не длиннее 200 знаков;
  • тумблер в тексте — тем же значком, что на его кнопке (✅ / ☑️).

Таблица статусов — только значки: слово у каждого состояния своё и
передаётся аргументом st(). Маркеры строк итога — отдельно: ✅ готово,
🔴 не выполнено, ⚠️ предупреждение и переспрос, ⛔ отказ, ℹ️ справка.
"""
from __future__ import annotations

import html as _html
import re as _re

STATUS = {"ok": "🟢", "warn": "🟡", "bad": "🔴", "wait": "⏳", "off": "⚪"}
TOAST_MAX = 200                  # предел всплывашки Telegram
TEXT_MAX = 4096                  # предел текста сообщения

_TAG = _re.compile(r"<[^>]+>")


def st(kind: str, word: str = "") -> str:
    """«🟢 работает», «⚪ нет связи», «⏳» — значок состояния и его слово."""
    icon = STATUS[kind]
    return f"{icon} {word}" if word else icon


def tick(on: bool) -> str:
    """Тумблер в тексте: ✅ включено / ☑️ выключено — как на кнопке."""
    return "✅" if on else "☑️"


def head(title: str, status: str = "", meta=()) -> str:
    """Шапка экрана: «🔔 <b>Уведомления</b>», «⬆️ <b>Обновления</b> · v3.2.0 🟢 актуальна»,
    «💾 <b>Бэкапы</b> ✅ вкл · 🔐 фраза задана». title — значок и имя через пробел
    (имя уже экранировано вызывающим, если пришло с хоста); status — вплотную
    к имени через пробел; meta — через « · »."""
    icon, _, name = title.partition(" ")
    out = f"{icon} <b>{name}</b>" if name else f"<b>{icon}</b>"
    if status:
        out += f" {status}"
    for m in meta:
        if m:
            out += f" · {m}"
    return out


def screen(title: str, status: str = "", *, meta=(), lines=(), details: str = "",
           note: str = "") -> str:
    """Экран вида «шапка + строки + подробнее»: lines — строки экрана (None —
    пропустить, "" — пустая строка); details — свёрнутый абзац; note — итог
    только что сделанного первой строкой через пустую (как screens.with_note)."""
    from awgbot.bot.texts.fmt import details as _details
    body = [head(title, status, meta)]
    body += [ln for ln in lines if ln is not None]
    text = "\n".join(body)
    if details:
        text += "\n" + _details(details)
    return f"{note}\n\n{text}" if note else text


def toast(text: str, limit: int = TOAST_MAX) -> str:
    """Текст всплывашки: теги сняты, сущности раскрыты, не длиннее limit — с «…»."""
    plain = _html.unescape(_TAG.sub("", str(text or "")))
    return plain if len(plain) <= limit else plain[:limit - 1] + "…"


def note_budget(screen_text: str, reserve: int = 300) -> int:
    """Сколько знаков остаётся под итог первой строкой экрана: 4096 минус
    видимый текст экрана и запас на хвост и «…и ещё N строк»."""
    visible = _html.unescape(_TAG.sub("", screen_text))
    return max(400, TEXT_MAX - len(visible) - reserve)
