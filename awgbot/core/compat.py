"""compat.py — линия совместимости установки.

COMPAT_FLOOR — версия, ниже которой этот код не возвращается и чьих копий не
принимает: резервная копия ниже неё не применяется, `awg-bot update` на
поставку ниже неё отказывает (откат — только чистой установкой). Одна
константа на код и скрипты: `awg-bot.sh` читает её из этого файла.

Миграций и переходов для копий нет: восстановление возможно только в своей
линии совместимости и на коде не старше копии.
"""
from __future__ import annotations

COMPAT_FLOOR = "3.4.0"


def version_key(raw: str) -> tuple | None:
    """«3.4.0» → (3, 4, 0); четвёртая цифра — хотфикс; нечисловое — None.
    Копию с кодом сравнивают по X.Y.Z."""
    from awgbot.infra.updates.model import parse_version
    return parse_version(str(raw or ""))


def below_floor(version: str) -> bool:
    """Версия ниже COMPAT_FLOOR (или не читается)."""
    have = version_key(version)
    return have is None or have[:3] < version_key(COMPAT_FLOOR)[:3]


def newer_than(version: str, code: str) -> bool:
    """Копия новее кода по X.Y.Z (хотфикс поверх той же версии — не новее)."""
    a, b = version_key(version), version_key(code)
    return a is not None and b is not None and a[:3] > b[:3]
