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
- `peeradmin` — роль админа второго сервера (и общий админ обоих серверов,
  и он же клиент этого — «👤 Меню клиента»);
- `unpair` — удаление пары;
- `package` — пакет переезда: отметки, сборка, досылка, приём;
- `guest` — бот основы на резерве: снимки, состояние, отказы хостовых сервисов;
- `dns` — запись DNS: провайдер, смена, факт, надзор;
- `outbound` — «мои клиенты»: переезд кнопкой, возврат, слияние;
- `inbound` — «клиенты основы у меня»: процесс открытия, зона, handback;
- `notify` — уведомления клиентам о переезде и возврате;
- `traffic` — итоги трафика через резерв;
- `away` — отказы при away, строка главной;
- `gwside` — постоянные линки моих шлюзов к резерву: ключи шлюзовых сторон,
  обмен `slots`/`slots_ok`, раздел `reserve` документа шлюза, взгляды шлюзов
  из снимков, Т-РУ18;
- `zonelinks` — стороны линков шлюзов основы в гостевой зоне (резерв) и
  подъём оболочки зоны после перезагрузки хоста;
- `vote` — матрица случаев, набор голосующих, `need` → `share` → процесс
  открытия, строка Н4 и приём аварийного кода;
- `codewait` — итог аварийного кода ответом на сообщение: короткое ожидание
  первого шага процесса открытия;
- `rfblock` — случаи 3 и Н3: демпфер, `view`/`ask`/`ask_no`, согласие основы;
- `rfzone` — РФ-доступ клиентов основы в зоне: запасной слот «шлюзы резерва»
  у гостя, разрешение резерва;
- `tgpool` — пул адресов Telegram для порта вебхука: анонсы AS Telegram,
  обучение по SYN, Т-РУ25.
"""
from __future__ import annotations

from .away import ReserveAwayMixin
from .dns import ReserveDnsMixin
from .guest import ReserveGuestMixin
from .gwside import ReserveGwSideMixin
from .inbound import ReserveInboundMixin
from .invite import ReserveInviteMixin
from .keys import ReserveKeysMixin
from .liveness import ReserveLivenessMixin
from .notify import ReserveNotifyMixin
from .outbound import ReserveOutboundMixin
from .package import ReservePackageMixin
from .pairing import ReservePairingMixin
from .peeradmin import ReservePeerAdminMixin
from .rfblock import ReserveRfBlockMixin
from .rfzone import ReserveRfZoneMixin
from .tgpool import ReserveTgPoolMixin
from .screen import ReserveScreenMixin
from .session import ReserveSessionMixin
from .state import ReserveStateMixin
from .traffic import ReserveTrafficMixin
from .unpair import ReserveUnpairMixin
from .vote import ReserveVoteMixin
from .zonelinks import ReserveZoneLinksMixin


class ReserveMixin(ReserveStateMixin, ReserveKeysMixin, ReserveInviteMixin, ReservePairingMixin,
                   ReserveSessionMixin, ReserveLivenessMixin, ReserveScreenMixin, ReservePeerAdminMixin, ReservePackageMixin, ReserveUnpairMixin,
                   ReserveGuestMixin, ReserveDnsMixin, ReserveOutboundMixin, ReserveInboundMixin, ReserveNotifyMixin,
                   ReserveTrafficMixin, ReserveAwayMixin, ReserveGwSideMixin, ReserveZoneLinksMixin, ReserveVoteMixin,
                   ReserveRfBlockMixin, ReserveRfZoneMixin, ReserveTgPoolMixin):
    """Резерв серверов: всё о паре — в одном месте сервисов."""


__all__ = ["ReserveMixin"]
