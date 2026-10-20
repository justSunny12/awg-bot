"""common.py — общее для сторон канала пары: сессия, ключ подписи, адреса, потолки."""
from __future__ import annotations

import asyncio
import ipaddress
import logging

from awgbot.core import settings
from awgbot.util import gwlink, pairproto

log = logging.getLogger(__name__)

DEFAULT_PORT = 8788          # порт канала пары внутри линка (канал шлюзов — 8787)
HELLO_TIMEOUT = 15
SEND_TIMEOUT = 30
_BACKOFF = (5, 10, 20, 40, 80, 160, 300)


def channel_port() -> int:
    return int(settings.get_int("app.reserve.pair_channel_port", DEFAULT_PORT) or DEFAULT_PORT)


def pair_hosts(cidr: str) -> tuple[str, str]:
    """(адрес принявшего, адрес пригласившего) в /30 линка пары: первый и
    второй хост — так их раздаёт скрипт линка пары."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return "", ""
    if net.version != 4 or net.prefixlen != 30:
        return "", ""
    hosts = [str(h) for h in net.hosts()]
    return hosts[0], hosts[1]


def channel_key(secret_b64: str) -> bytes:
    """Ключ подписи канала из общего секрета линка пары: PSK после сопряжения,
    закрытый ключ стороны принявшего — до него (он в приглашении и у
    пригласившего, который его сгенерировал)."""
    return pairproto.pair_key(secret_b64)


class _Session:
    """Всё, что живёт ровно одну сессию канала пары."""

    def __init__(self, writer: asyncio.StreamWriter, key: bytes, *, listener: bool):
        self.writer = writer
        self.key = key
        self.listener = listener         # эта сторона — слушатель (пригласивший)
        self.seq = 0
        self.hello = False
        self.pairing = False             # сессия сопряжения (hello с pair)
        self.sid = ""
        self.caps: list[str] = []
        self.peer_name = ""
        self.sn = gwlink.new_nonce()     # наш нонс: им подписывает нам другая сторона
        self.cn = b""                    # нонс другой стороны: им подписываем мы
        self.sn_sent = False
        self.st_sent: dict | None = None
        self.parts: dict = {}            # сборка пакета частями: {gen: [часть…]}


async def send_line(sess: _Session, kind: str, body: dict | None = None, *, pad: int = gwlink.PAD_DELTA) -> bool:
    """Одно сообщение в сессию: подпись нонсом получателя, первый раз — свой нонс в теле."""
    sess.seq += 1
    body = dict(body or {})
    if not sess.sn_sent:
        body["nonce"] = gwlink.nonce_b64(sess.sn)
        sess.sn_sent = True
    line = gwlink.pack(sess.key, kind, body, seq=sess.seq, pad=pad, nonce=sess.cn)
    try:
        sess.writer.write(line)
        await asyncio.wait_for(sess.writer.drain(), SEND_TIMEOUT)
        return True
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        log.info("канал пары: «%s» не ушло (%s)", kind, e)
        return False


async def close_session(sess: _Session) -> None:
    import contextlib
    with contextlib.suppress(Exception):
        sess.writer.close()
        try:
            await asyncio.wait_for(sess.writer.wait_closed(), 5)
        except asyncio.TimeoutError:
            sess.writer.transport.abort()
