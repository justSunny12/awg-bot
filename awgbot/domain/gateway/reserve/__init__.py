"""
reserve — резерв серверов на стороне агента шлюза.

- `uplink` — перерезолв аплинка: адрес для клиентов переехал на резерв, аплинк
  идёт на новый адрес сам (работает и без пары);
- `store` — раздел `reserve` документа: файл reserve.json, линк `awgrsv`
  скриптом, решение о пересылке, поле `rsv` снимка;
- `watch` — взгляд на основу (линк слота, проба по каналу и по аплинку) и на
  резерв (awgrsv, проба по гостевому каналу);
- `vote` — гостевой канал: hello/seen/share/pong, входящие view/need/state/ping;
- `record` — факт записи DNS у авторитетного сервера.
"""
from __future__ import annotations

from . import record, uplink
from .store import ReserveStoreMixin
from .vote import ReserveVoteMixin
from .watch import ReserveWatchMixin


class GatewayReserveMixin(ReserveStoreMixin, ReserveWatchMixin, ReserveVoteMixin):
    """Резерв у агента шлюза — одним миксином в GatewayServices."""


__all__ = ["uplink", "record", "GatewayReserveMixin", "ReserveStoreMixin", "ReserveWatchMixin", "ReserveVoteMixin"]
