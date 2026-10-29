"""
quorum — кворум взаимного резерва серверов на стороне основного бота
(концепт «кворум», 4.2.0). Части:

- `members`  — состав своей линии и поколение (`rsv_quorum`), подтверждение
               второго сервера (`qmem`/`qmem_ok`), сторона линии, цифры времени,
               почему кворум не включён, поле `quo` в `st`;
- `keys`     — ключи кворума (`rsv_qkeys`): многочлен `Mq`, прямая кода `Mc`,
               ящики, `qkeys`/`qkeys_ok`, точки шлюзам, включение и выключение;
- `probes`   — пиры-зонды и второй адрес на интерфейсе AWG (`rsv_probes`),
               конфиги зондов второму серверу (`qprobe`) и своим шлюзам
               (раздел `quorum` документа), свои интерфейсы зондов в `awg-quo`;
- `leader`   — ответчик и счёт «да» (правило `util/quorum/leader`) у лидера
               своей линии — на основе дома и у бота основы на резерве;
- `voter`    — свои пробы как голосующего о второй линии и о своём клоне
               (правило `util/quorum/voter`), взгляды, `qview`;
- `inbound`  — слова каналов: `qprobe`, `qview`, `qstate` по каналу пары,
               `qview` шлюзов по гостевому каналу и каналу конфигурации;
- `candidate` — правило кандидата о линии второй стороны и исполнение решения
               (просьба лидеру, точки через ящик кворума, окно кода);
- `fence`    — решения лидера: ограждение и возврат через журнал, старт без
               права записи, принятие чужой эпохи, плановая пауза, загородка бота;
- `tie`      — ничья: тревога обоим админам и отбой (Т-К5…Т-К6);
- `returns`  — возвраты по кворуму: «сразу» по Q2, аварийный по Q1 о клоне,
               корреляция голосующих (Т-К15);
- `screen`   — данные карточки «🛟 Резерв» и экрана роли;
- `tick`     — такт кворума и план сокетов для runtime/quorum.

Машины правил — чистые (util/quorum); здесь — база, сеть через действия
runtime (`runtime/quorum.py` держит сокеты ответчика и проб).
"""
from __future__ import annotations

from .candidate import ReserveQuorumCandidateMixin
from .fence import ReserveQuorumFenceMixin
from .inbound import ReserveQuorumInboundMixin
from .keys import ReserveQuorumKeysMixin
from .leader import ReserveQuorumLeaderMixin
from .members import ReserveQuorumMembersMixin
from .probes import ReserveQuorumProbesMixin
from .returns import ReserveQuorumReturnsMixin
from .screen import ReserveQuorumScreenMixin
from .tie import ReserveQuorumTieMixin
from .tick import ReserveQuorumTickMixin
from .voter import ReserveQuorumVoterMixin


class ReserveQuorumMixin(ReserveQuorumMembersMixin, ReserveQuorumKeysMixin, ReserveQuorumProbesMixin, ReserveQuorumLeaderMixin,
                         ReserveQuorumVoterMixin, ReserveQuorumInboundMixin, ReserveQuorumScreenMixin,
                         ReserveQuorumCandidateMixin, ReserveQuorumFenceMixin, ReserveQuorumTieMixin,
                         ReserveQuorumReturnsMixin, ReserveQuorumTickMixin):
    """Кворум резерва — одним миксином в ReserveMixin."""


__all__ = ["ReserveQuorumMixin"]
