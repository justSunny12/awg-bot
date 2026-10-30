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
  и он же пользователь этого — «👤 Меню пользователя»);
- `unpair` — удаление пары;
- `package` — пакет переезда: отметки, сборка, досылка, приём;
- `guest` — бот основы на резерве: снимки, состояние, отказы хостовых сервисов;
- `dns` — запись DNS: провайдер, смена, факт, надзор;
- `outbound` — «мои пользователи»: переезд кнопкой, away, здоровье, зависший
  вебхук резерва;
- `outbound_stuck` — «мои пользователи»: зависший вебхук резерва (away и home);
- `outbound_return` — «мои пользователи»: возврат с резерва (в том числе без
  реплики и рычагом «вернуть без резерва»);
- `merge_ops` — слияние реплики при возврате: базы, пиры, блокировки, отчёт;
- `inbound` — «пользователи основы у меня»: автомат приёма, зона, handback;
- `inbound_back` — их возврат основе (`back`), пересылка хвоста, relay;
- `openproc` — запуск процесса открытия временным юнитом (`OPEN_UNIT`,
  свойства песочницы `rsvopen_sandbox_props`), код завершения и его stderr;
- `notify` — уведомления пользователям о переезде и возврате;
- `traffic` — итоги трафика через резерв;
- `away` — отказы при away, строка главной;
- `gwside` — постоянные линки моих шлюзов к резерву: ключи шлюзовых сторон,
  обмен `slots`/`slots_ok`, раздел `reserve` документа шлюза, взгляды шлюзов
  из снимков, Т-РУ18;
- `zonelinks` — стороны линков шлюзов основы в гостевой зоне (резерв) и
  подъём оболочки зоны после перезагрузки хоста;
- `gwguest` — гостевой канал шлюзов основы у резерва: сессии, живость, `state`
  и эпоха линии первым словом;
- `opening` — предусловия и запуск процесса открытия по точкам кворума или по
  аварийному коду (`reserve_open_block`, `reserve_auto_block`);
- `codewin` — окно аварийного кода (ведёт кандидат кворума) и приём кода;
- `codewait` — итог аварийного кода ответом на сообщение: короткое ожидание
  первого шага процесса открытия;
- `rfzone` — РФ-доступ пользователей основы в зоне: запасной слот «шлюзы резерва»
  у гостя, разрешение резерва;
- `tgpool` — пул адресов Telegram для порта вебхука: анонсы AS Telegram,
  обучение по SYN, Т-РУ25;
- `journal` — эпоха линии данных и следование основы, пока её пользователи у
  резерва (пакет);
- `quorum` — кворум: состав линии, пиры-зонды и конфиги зондов, ответчик и
  счёт «да», свои пробы и взгляды, слова каналов, данные карточки (пакет).
"""
from __future__ import annotations

from .away import ReserveAwayMixin
from .codewin import ReserveCodeWindowMixin
from .dns import ReserveDnsMixin
from .guest import ReserveGuestMixin
from .gwside import ReserveGwSideMixin
from .inbound import ReserveInboundMixin
from .inbound_back import ReserveInboundBackMixin
from .journal import ReserveJournalMixin
from .invite import ReserveInviteMixin
from .keys import ReserveKeysMixin
from .liveness import ReserveLivenessMixin
from .merge_ops import ReserveMergeOpsMixin
from .notify import ReserveNotifyMixin
from .openproc import ReserveOpenProcMixin
from .outbound import ReserveOutboundMixin
from .outbound_return import ReserveReturnMixin
from .outbound_stuck import ReserveStuckMixin
from .package import ReservePackageMixin
from .pairing import ReservePairingMixin
from .peeradmin import ReservePeerAdminMixin
from .quorum import ReserveQuorumMixin
from .rfzone import ReserveRfZoneMixin
from .tgpool import ReserveTgPoolMixin
from .screen import ReserveScreenMixin
from .session import ReserveSessionMixin
from .state import ReserveStateMixin
from .traffic import ReserveTrafficMixin
from .unpair import ReserveUnpairMixin
from .gwguest import ReserveGwGuestMixin
from .opening import ReserveOpeningMixin
from .zonelinks import ReserveZoneLinksMixin


class ReserveMixin(ReserveStateMixin, ReserveKeysMixin, ReserveInviteMixin, ReservePairingMixin,
                   ReserveSessionMixin, ReserveLivenessMixin, ReserveScreenMixin, ReservePeerAdminMixin, ReservePackageMixin, ReserveUnpairMixin,
                   ReserveGuestMixin, ReserveDnsMixin, ReserveOutboundMixin, ReserveInboundMixin, ReserveNotifyMixin,
                   ReserveTrafficMixin, ReserveAwayMixin, ReserveGwSideMixin, ReserveZoneLinksMixin, ReserveGwGuestMixin,
                   ReserveOpeningMixin, ReserveRfZoneMixin, ReserveTgPoolMixin, ReserveMergeOpsMixin,
                   ReserveCodeWindowMixin, ReserveReturnMixin, ReserveOpenProcMixin, ReserveJournalMixin,
                   ReserveInboundBackMixin, ReserveStuckMixin, ReserveQuorumMixin):
    """Резерв серверов: всё о паре — в одном месте сервисов."""


__all__ = ["ReserveMixin"]
