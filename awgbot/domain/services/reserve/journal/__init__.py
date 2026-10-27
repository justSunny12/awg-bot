"""
journal — эпоха и следование (журнал данных резерва) на стороне основного бота.

Части:
- `epoch` — эпоха линии данных: передачи права записи по фактам (передача
  бота, 409 и чужой вебхук, переезд мимо меня; возврат, «вернуть без
  резерва», срыв переезда) и страховка «пользователи и Telegram здесь —
  держатель здесь»;
- `follow` — основа в следовании: блокировки держателя для её оставшихся
  сессий, отложенный месячный сброс, отказ базы словами раздела.

Запрет записи решений у следующего — в самой базе (util/rsvjrn).
"""
from __future__ import annotations

from .epoch import ReserveEpochMixin
from .follow import ReserveFollowMixin


class ReserveJournalMixin(ReserveEpochMixin, ReserveFollowMixin):
    """Эпоха и следование."""


__all__ = ["ReserveJournalMixin"]
