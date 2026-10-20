"""
reserve — взаимный резерв серверов AWG на стороне основного бота.

Части (по модулю на заботу, общий замок — `state._LOCK`):
- `state` — строка пары в БД, ключи `rsv_*`, состояния направлений;
- `compat` — возможности пары и матрица готовности направлений;
- `keys` — главный ключ `M`, ключ пакета `K`, поколения, доли и аварийный код;
- `invite` — приглашение в пару и его отмена (пригласивший);
- `pairing` — сопряжение кодом с обеих сторон;
- `unpair` — удаление пары.

Дальше по этапам: живость линка пары, пакет, переезд и возврат, голосование,
роль админа второго сервера, пул адресов Telegram.
"""
from __future__ import annotations

from .invite import ReserveInviteMixin
from .keys import ReserveKeysMixin
from .liveness import ReserveLivenessMixin
from .pairing import ReservePairingMixin
from .peeradmin import ReservePeerAdminMixin
from .screen import ReserveScreenMixin
from .session import ReserveSessionMixin
from .state import ReserveStateMixin
from .unpair import ReserveUnpairMixin


class ReserveMixin(ReserveStateMixin, ReserveKeysMixin, ReserveInviteMixin, ReservePairingMixin,
                   ReserveSessionMixin, ReserveLivenessMixin, ReserveScreenMixin, ReservePeerAdminMixin, ReserveUnpairMixin):
    """Резерв серверов: всё о паре — в одном месте сервисов."""


__all__ = ["ReserveMixin"]
