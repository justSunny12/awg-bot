"""common.py — общее для частей клиента канала: журнал, порт по умолчанию, задержки переподключения, предел сдачи."""
from __future__ import annotations

import logging

from awgbot.util import gwlink

log = logging.getLogger(__name__)

# Возможности агента — контракт канала списком имён, не номером версии: сервер
# проверяет одним `in`. Пустой список — агент прежних версий.
# cfg1 — документ cfg / cfg_ack; confirm1 — подтверждение в чате (ключи линка,
# аплинк); token1 — раздел token (токен бота шлюза); keep1 — копия настроек
# шлюза на сервере (keep / keep_ok / keep_back / keep_none); upd1 — команда
# набора обновлений (upd / upd_ack / upd_commit / upd_revert)
CAPS: tuple[str, ...] = ("cfg1", "confirm1", "token1", "keep1", "upd1", "rekey2", "join1", "rsv1")

DEFAULT_PORT = gwlink.DEFAULT_PORT
# 5 с → 15 → 45 → 135 → 300, каждый шаг × uniform(0.6, 1.4). Потолок пять минут:
# линк лежит редко и надолго, а долбиться в него чаще незачем.
_BACKOFF = (5, 15, 45, 135, 300)
SEND_TIMEOUT = 30       # с — drain не дождался: ВПС не читает, сессия мёртвая
