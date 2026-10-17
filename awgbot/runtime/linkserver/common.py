"""common.py — общее для частей слушателя: предел ожидания hello, журнал, сессия слота."""
from __future__ import annotations

import asyncio
import logging

from awgbot.util import gwlink

HELLO_TIMEOUT = 15       # с: первое слово шлюза, иначе коннект отбрасывается без вытеснения живой сессии

log = logging.getLogger(__name__)

DEFAULT_PORT = gwlink.DEFAULT_PORT

# Возможности сервера — уходят агенту первым сообщением (`role`): по ним он
# понимает, что сервер пришлёт сам. Пустой список — сервер прежних версий.
# cfg1 — документ cfg / cfg_ack; confirm1 — подтверждение в чате (ключи линка,
# аплинк); token1 — раздел token (токен бота шлюза); keep1 — копия настроек
# шлюза (keep / keep_ok / keep_back / keep_none); restore1 — бэкап сервера из чата
# бота шлюза (rst_part / rst_done)
SERVER_CAPS: tuple[str, ...] = ("cfg1", "confirm1", "token1", "keep1", "restore1")

class _Session:
    """Всё, что живёт ровно одну сессию слота. Одно место сброса вместо шести
    параллельных словарей: новое «уже отправили в этой сессии» иначе однажды
    забыли бы сбросить, и оно пережило бы переподключение шлюза."""

    def __init__(self, writer: asyncio.StreamWriter, key: bytes):
        self.writer = writer
        self.key = key
        self.seq = 0
        self.hello = False               # был ли принят hello — только тогда сессия «была»
        self.lists_have = ""             # отпечаток фидов, который шлюз назвал своим
        self.lists_sent = ""             # отпечаток фидов, уже ушедший в этой сессии
        self.svc_have = ""               # отпечаток записей соседей, который шлюз назвал своим
        self.svc_sent: str | None = None # отпечаток записей соседей, ушедший в этой сессии
        self.own_have: str | None = None # отпечаток канона своих списков у шлюза; None — агент синхронизацию не знает
        self.own_sent: str | None = None # отпечаток канона, ушедший в этой сессии
        self.role_sent: dict | None = None
        self.caps: list[str] = []        # возможности агента из hello (контракт канала); пусто — агент до 3.3.0
        self.cfg_sent = ""               # отпечаток документа конфигурации, ушедшего в этой сессии
        self.rekey_sent = ""             # id перехода ключей, чей link_next ушёл в этой сессии
        self.rst = None                  # сборка бэкапа из чата шлюза (rst_part): обрыв сессии её стирает
        # вытеснила сессию, прошедшую hello: запись «на связи» в БД — её, и
        # закрыть её обязана эта, даже если сама до hello не дойдёт
        self.took_over = False
        self.sn = gwlink.new_nonce()     # наш нонс: им шлюз подписывает всё после hello
        self.cn = b""                    # нонс шлюза из hello: им подписываем мы
        self.sn_sent = False             # первое наше сообщение несёт sn


