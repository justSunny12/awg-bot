"""
enums.py — типизированные значения статусных полей.

StrEnum (Python 3.11+): члены СРАВНИМЫ со строками и сериализуются как строки,
поэтому в SQLite хранятся ровно теми же значениями ('active', 'user', ...), что и
раньше — миграция не нужна, обратная совместимость полная. Выигрыш: единый
источник значений, защита от опечаток, автодополнение вместо «магических строк».

Использование:
    if client.subscription.status == SubStatus.EXPIRED: ...
    db.update_client_fields(cid, status=SubStatus.ACTIVE)   # запишется 'active'
"""
from __future__ import annotations

from enum import StrEnum


class SubStatus(StrEnum):
    """client_subscription.status — жив ли биллинг-цикл."""
    ACTIVE = "active"
    EXPIRED = "expired"


class ActivationStatus(StrEnum):
    """clients.activation_status — активирован ли инвайт."""
    PENDING = "pending"
    ACTIVE = "active"


class PauseMode(StrEnum):
    """client_pause.pause_mode — кто и как приостановил подписку."""
    USER = "user"                 # клиент сам, срочная (тикает reserved_days)
    ADMIN_FIXED = "admin_fixed"   # админ на фикс. срок
    ADMIN_OPEN = "admin_open"     # админ бессрочно (temp-бессрочный режим)


class PeriodKind(StrEnum):
    """client_subscription.period_kind — тип периода подписки."""
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    NEVER = "never"               # бессрочно (period_end NULL)


class FriendStatus(StrEnum):
    """device_friend.friend_status — состояние гостевого доступа."""
    PENDING = "pending"           # инвайт выдан, друг не активировал
    ACTIVE = "active"             # друг подключён


__all__ = ["SubStatus", "ActivationStatus", "PauseMode", "PeriodKind",
           "FriendStatus"]
