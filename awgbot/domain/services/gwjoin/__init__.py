"""
gwjoin/ — подключение шлюза одной командой (только основной бот; §3.6–3.9
концепта «единые обновления»).

Части: state — запись сопряжения `gw_join_<слот>` и то, что живёт только в
памяти процесса (эфемерные ключи, код, ключ сопряжения); issue — команда:
слот, линк слота на ключах токена (`--join-stage`), токен; pairing — сессия
сопряжения: hello{join,eph} → join_eph, код, попытки, срок, отмена; accept —
верный код: строка слота (новое устройство, замена, «🔁 Настроить заново»),
секреты join_ok, смена ключей `auto=join`, фиксация и итог; возврат при
отмене, сроке и третьем неверном коде — сразу (Р24); quarantine — признак
сопряжения для автомата, политики, доставок и транзита.

Карантин держат три рубежа: строки нового слота нет в `gateways` до кода;
файервол ВПС — по штампу «сопряжение» в конфиге линка (infra/nftguard);
слушатель канала разбирает в сессии сопряжения только её сообщения
(runtime/linkserver/join.py).
"""
from __future__ import annotations

from .state import GwJoinStateMixin
from .issue import GwJoinIssueMixin
from .pairing import GwJoinPairingMixin
from .accept import GwJoinAcceptMixin
from .quarantine import GwJoinQuarantineMixin


class GwJoinMixin(GwJoinStateMixin, GwJoinIssueMixin, GwJoinPairingMixin, GwJoinAcceptMixin,
                  GwJoinQuarantineMixin):
    """Подключение шлюза командой — подмешивается в Services основного бота."""


__all__ = ["GwJoinMixin"]
