"""
reserve — взаимный резерв серверов AWG на стороне основного бота.

Части (по модулю на заботу, общий замок — `state._LOCK`):
- `state` — строка пары в БД, ключи `rsv_*`, состояния направлений;
- `compat` — возможности пары и матрица готовности направлений;
- `keys` — главный ключ `M`, ключ пакета `K`, поколения, доли и аварийный код;
- `invite` — приглашение в пару и его отмена (пригласивший);
- `pairing` — сопряжение кодом с обеих сторон;
- `session` — что сервисы отдают каналу пары (`hello`, `st`) и принимают из
  него, готовность направлений;
- `liveness` — живость линка пары (пропуски keepalive, проба по подозрению);
- `screen` — данные экранов раздела «🛟 Резерв»;
- `peeradmin` — роль админа второго сервера;
- `unpair` — удаление пары.

Дальше по этапам: пакет, переезд и возврат, голосование, пул адресов Telegram.
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
