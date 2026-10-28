"""
journal — эпоха и следование (журнал данных резерва) на стороне основного бота.

Части:
- `epoch` — эпоха линии данных: передачи права записи по фактам (передача
  бота, 409 и чужой вебхук, переезд мимо меня; возврат, «вернуть без
  резерва», срыв переезда) и страховка «пользователи и Telegram здесь —
  держатель здесь»;
- `follow` — основа в следовании: блокировки держателя для её оставшихся
  сессий, отложенный месячный сброс, отказ базы словами раздела; применение
  событий и счётчиков держателя, пиры и блокировки по ним, сводка эпохи;
- `ship` — своя линия к резерву: захват по направлению, сверка до первой
  отправки, события и пачки счётчиков, подтверждения, снимок по правилам;
- `keep` — сторона резерва: журнал линии второй стороны на диске, снятие по
  снимку, её записи — и её боту у меня;
- `route` — разбор сообщений журнала по линии (моя / второй стороны), транзит
  записей гостя;
- `handover` — передача права записи при переезде кнопкой и возврате без
  реплики и слияния;
- `tail` — хвост старой эпохи: отправка бывшим держателем, перебазирование у
  держателя, `jtail_ok` со списком спорного, Т-Ж4/Т-Ж5 (только у основы), отказ
  обновления, пока хвост не пришёл;
- `guestj` — бот основы на резерве с журналом: свой журнал данных на диске,
  записи основе, следование до передачи бота, возврат без реплики.

Запрет записи решений у следующего — в самой базе (util/rsvjrn).
"""
from __future__ import annotations

from .epoch import ReserveEpochMixin
from .follow import ReserveFollowMixin
from .guestj import ReserveGuestJournalMixin
from .handover import ReserveHandoverMixin
from .tail import ReserveTailMixin
from .keep import ReserveKeepMixin
from .route import ReserveJournalRouteMixin
from .ship import ReserveShipMixin


class ReserveJournalMixin(ReserveEpochMixin, ReserveFollowMixin, ReserveShipMixin, ReserveKeepMixin,
                          ReserveJournalRouteMixin, ReserveGuestJournalMixin, ReserveHandoverMixin, ReserveTailMixin):
    """Эпоха, следование, доставка и хранение журнала."""


__all__ = ["ReserveJournalMixin"]
