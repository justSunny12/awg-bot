"""
quorum — кворум взаимного резерва серверов на стороне основного бота
(концепт «кворум», 4.2.0). Части:

- `members`  — состав своей линии и поколение (`rsv_quorum`), сторона линии,
               цифры времени, почему кворум не включён, поле `quo` в `st`;
- `probes`   — пиры-зонды и второй адрес на интерфейсе AWG (`rsv_probes`),
               конфиги зондов второму серверу (`qprobe`) и своим шлюзам
               (раздел `quorum` документа), свои интерфейсы зондов в `awg-quo`;
- `leader`   — ответчик и счёт «да» (правило `util/quorum/leader`) у лидера
               своей линии — на основе дома и у бота основы на резерве;
- `voter`    — свои пробы как голосующего о второй линии и о своём клоне
               (правило `util/quorum/voter`), взгляды, `qview`;
- `inbound`  — слова каналов: `qprobe`, `qview`, `qstate` по каналу пары,
               `qview` шлюзов по гостевому каналу и каналу конфигурации;
- `screen`   — данные карточки «🛟 Резерв» и экрана роли;
- `tick`     — такт кворума и план сокетов для runtime/quorum.

Машины правил — чистые (util/quorum); здесь — база, сеть через действия
runtime (`runtime/quorum.py` держит сокеты ответчика и проб).
"""
from __future__ import annotations

from .inbound import ReserveQuorumInboundMixin
from .leader import ReserveQuorumLeaderMixin
from .members import ReserveQuorumMembersMixin
from .probes import ReserveQuorumProbesMixin
from .screen import ReserveQuorumScreenMixin
from .tick import ReserveQuorumTickMixin
from .voter import ReserveQuorumVoterMixin


class ReserveQuorumMixin(ReserveQuorumMembersMixin, ReserveQuorumProbesMixin, ReserveQuorumLeaderMixin,
                         ReserveQuorumVoterMixin, ReserveQuorumInboundMixin, ReserveQuorumScreenMixin,
                         ReserveQuorumTickMixin):
    """Кворум резерва — одним миксином в ReserveMixin."""


__all__ = ["ReserveQuorumMixin"]
