"""
reserve — резерв серверов на стороне агента шлюза.

- `uplink` — перерезолв аплинка: адрес для пользователей переехал на резерв, аплинк
  идёт на новый адрес сам (работает и без пары);
- `store` — раздел `reserve` документа: файл reserve.json, линк `awgrsv`
  скриптом, решение о пересылке, поле `rsv` снимка;
- `watch` — связь с резервом (awgrsv, проба по гостевому каналу); взгляда на
  основу здесь нет — это зонд кворума;
- `vote` — гостевой канал: hello/pong, входящие state/ping и слова кворума;
- `record` — факт записи DNS у авторитетного сервера;
- `quorum` — кворум: раздел `quorum` документа, зонды в `awg-quo`, взгляд
  голосующего, `qview`, точка ключа кворума (`qshare`), панель и снимок;
- `tgvia` — выход агента в Telegram через `awgrsv` и немедленный перерезолв
  аплинка.
"""
from __future__ import annotations

from . import record, uplink
from .quorum import GatewayQuorumMixin
from .store import ReserveStoreMixin
from .tgvia import ReserveTgViaMixin
from .vote import ReserveVoteMixin
from .watch import ReserveWatchMixin


class GatewayReserveMixin(ReserveStoreMixin, ReserveWatchMixin, ReserveVoteMixin, GatewayQuorumMixin, ReserveTgViaMixin):
    """Резерв у агента шлюза — одним миксином в GatewayServices."""


__all__ = ["uplink", "record", "GatewayReserveMixin", "ReserveStoreMixin", "ReserveWatchMixin", "ReserveVoteMixin"]
