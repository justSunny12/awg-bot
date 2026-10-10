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
SERVER_CAPS: tuple[str, ...] = ("cfg1",)        # документ конфигурации cfg / cfg_ack

class _Session:
    """Всё, что живёт ровно одну сессию слота. Одно место сброса вместо шести
    параллельных словарей: новое «уже отправили в этой сессии» иначе однажды
    забыли бы сбросить, и оно пережило бы переподключение шлюза."""

    def __init__(self, writer: asyncio.StreamWriter, key: bytes):
        self.writer = writer
        self.key = key
        self.seq = 0
        self.hello = False               # был ли принят hello — только тогда сессия «была»
        self.sent_hash = ""              # набор настроек, уже отправленный в этой сессии
        self.sent_queue: list[str] = []  # отпечатки наборов в пути: агент отвечает по одному на каждый, по порядку
        self.lists_have = ""             # отпечаток фидов, который шлюз назвал своим
        self.lists_sent = ""             # отпечаток фидов, уже ушедший в этой сессии
        self.svc_have = ""               # отпечаток записей соседей, который шлюз назвал своим
        self.svc_sent: str | None = None # отпечаток записей соседей, ушедший в этой сессии
        self.own_have: str | None = None # отпечаток канона своих списков у шлюза; None — агент синхронизацию не знает
        self.own_sent: str | None = None # отпечаток канона, ушедший в этой сессии
        self.role_sent: dict | None = None
        self.caps: list[str] = []        # возможности агента из hello; пусто — агент прежних версий
        self.cfg_sent = ""               # отпечаток документа конфигурации, ушедшего в этой сессии
        # вытеснила сессию, прошедшую hello: запись «на связи» в БД — её, и
        # закрыть её обязана эта, даже если сама до hello не дойдёт
        self.took_over = False
        self.sn = gwlink.new_nonce()     # наш нонс: им шлюз подписывает всё после hello
        self.cn = b""                    # нонс шлюза из hello: им подписываем мы
        self.sn_sent = False             # первое наше сообщение несёт sn


