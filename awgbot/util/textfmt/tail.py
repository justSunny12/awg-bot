"""tail.py — часть пакета textfmt (см. __init__): хвост обрезанного списка.

Есть экран со всем списком — хвост ссылкой словами: «… и ещё 3» (палец
попадает в слово); нет — в строке-сводке «[+3]», в списке строками —
последней строкой словами. Молча не режем."""

from __future__ import annotations

from .base import deep_link


def more_link(n: int, bot_username: str, payload: str) -> str:
    """« и ещё N» в строке-сводке — ссылкой на экран со всем списком (без имени бота — текстом)."""
    return f" {deep_link(bot_username, payload, f'и ещё {int(n)}')}" if n > 0 else ""


def more_count(n: int) -> str:
    """« [+N]» — хвост без ссылки: полный список на кнопке или соседнем экране."""
    return f" [+{int(n)}]" if n > 0 else ""


def more_line(n: int, bot_username: str, payload: str) -> str:
    """Строка «… и ещё N» под списком строками — ссылкой."""
    return f"… {deep_link(bot_username, payload, f'и ещё {int(n)}')}" if n > 0 else ""
