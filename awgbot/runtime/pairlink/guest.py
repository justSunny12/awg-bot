"""
guest.py — гостевой канал на стороне резерва: слушатель TCP внутри гостевой
зоны (сетевое пространство `awg-guest`), к которому по постоянным линкам
`awglink<N>` приходят шлюзы основы. Конверт — `util/gwlink` с ключом из PSK
стороны зоны (`pairproto.guest_key`), словарь — `pairproto.GUEST_KINDS`.

Сокет создаётся в пространстве зоны (`netns.listen_in`), сам процесс бота
остаётся в пространстве хоста. Одна сессия на слот: новая вытесняет старую.
Нонсы — как у канала шлюзов: `hello` несёт нонс шлюза, первое сообщение
резерва — его нонс. В простое ни байта: `view` и `state` — по смене.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

from awgbot.util import gwlink, pairproto

log = logging.getLogger(__name__)

HELLO_TIMEOUT = 20
SEND_TIMEOUT = 20


class _GuestSession:
    def __init__(self, writer: asyncio.StreamWriter, key: bytes):
        self.writer = writer
        self.key = key
        self.slot = 0
        self.seq = 0
        self.sn = gwlink.new_nonce()
        self.cn = b""
        self.hello = False


class GuestChannelServer:
    """Слушатель гостевого канала у основного бота резерва."""

    def __init__(self, services, port: int):
        self.services = services
        self.port = int(port)
        self._server: asyncio.AbstractServer | None = None
        self._sessions: dict[int, _GuestSession] = {}

    def online_slots(self) -> list[int]:
        return [s for s, sess in self._sessions.items() if sess.hello and not sess.writer.is_closing()]

    async def ensure(self) -> None:
        if self._server is not None:
            return
        from awgbot.infra import rsvzone
        from awgbot.util import netns
        path = netns.path_of(rsvzone.ZONE)
        if not netns.exists(path):
            return

        def make() -> socket.socket:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", self.port))
            s.listen(16)
            s.setblocking(False)
            return s

        sock = await asyncio.to_thread(netns.listen_in, path, make)
        self._server = await asyncio.start_server(self._serve, sock=sock, limit=gwlink.MAX_LINE)
        log.info("гостевой канал: слушаю в зоне %s порт %s", rsvzone.ZONE, self.port)

    async def stop(self) -> None:
        for sess in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                sess.writer.close()
        self._sessions.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    # ── сессия ───────────────────────────────────────────────────────────────
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        addr = str(peer[0]) if peer else ""
        sess: _GuestSession | None = None
        try:
            key, slot = await asyncio.to_thread(self._key_for, addr)
            if not key:
                log.info("гостевой канал: соединение с %s — стороны зоны для него нет", addr)
                return
            sess = _GuestSession(writer, key)
            sess.slot = slot
            gwlink.tcp_keepalive(writer)
            first = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
            if not first:
                return
            msg = gwlink.unpack(key, first, last_seq=0)
            if str(msg.get("t") or "") != "hello":
                raise pairproto.ProtocolError("первым должно быть hello")
            body = pairproto.check(pairproto.GUEST_PROTO, "hello", msg, pairproto.GATEWAY)
            if int(body.get("slot") or 0) != slot:
                raise pairproto.ProtocolError("слот в hello не тот, что у стороны зоны")
            sess.cn = gwlink.nonce_from(msg.get("nonce"))
            last_seq = int(msg.get("seq") or 0)
            old = self._sessions.get(slot)
            if old is not None and old is not sess:
                with contextlib.suppress(Exception):
                    old.writer.close()
            self._sessions[slot] = sess
            sess.hello = True
            actions = await asyncio.to_thread(self.services.reserve_guest_opened, slot, body)
            await self._actions(actions)
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = gwlink.unpack(key, line, last_seq=last_seq, nonce=sess.sn)
                last_seq = int(msg.get("seq") or 0)
                kind = str(msg.get("t") or "")
                body = pairproto.check(pairproto.GUEST_PROTO, kind, msg, pairproto.GATEWAY)
                actions = await asyncio.to_thread(self.services.reserve_guest_in, slot, kind, body)
                await self._actions(actions)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            log.info("гостевой канал: сессия %s закрыта: %s", addr, e)
        except pairproto.ProtocolError as e:
            log.warning("гостевой канал: %s — %s; сессия разорвана", addr, e)
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # noqa: BLE001
            log.warning("гостевой канал: сессия %s: %s", addr, e)
        finally:
            if sess is not None and sess.hello and self._sessions.get(sess.slot) is sess:
                self._sessions.pop(sess.slot, None)
                with contextlib.suppress(Exception):
                    await self._actions(await asyncio.to_thread(self.services.reserve_guest_closed, sess.slot))
            with contextlib.suppress(Exception):
                writer.close()

    def _key_for(self, addr: str) -> tuple[bytes, int]:
        """Ключ канала и слот — по адресу шлюзовой стороны /30 (второй адрес)."""
        import ipaddress
        for key, rec in (self.services.reserve_guest_links() or {}).items():
            if not key.isdigit():
                continue
            try:
                hosts = list(ipaddress.ip_network(str(rec.get("cidr") or ""), strict=False).hosts())
            except ValueError:
                continue
            if len(hosts) >= 2 and str(hosts[1]) == addr and rec.get("psk"):
                return pairproto.guest_key(str(rec["psk"])), int(key)
        return b"", 0

    async def _actions(self, actions) -> None:
        from awgbot.runtime import rsvactions
        await rsvactions.run(actions)

    async def send(self, slot: int, kind: str, body: dict | None = None) -> bool:
        sess = self._sessions.get(int(slot))
        if sess is None or not sess.hello or sess.writer.is_closing():
            return False
        body = dict(body or {})
        if sess.seq == 0:
            body["nonce"] = gwlink.nonce_b64(sess.sn)
        sess.seq += 1
        try:
            sess.writer.write(gwlink.pack(sess.key, kind, body, seq=sess.seq, pad=gwlink.PAD_DELTA, nonce=sess.cn))
            await asyncio.wait_for(sess.writer.drain(), SEND_TIMEOUT)
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            log.info("гостевой канал: «%s» шлюзу %s не ушло: %s", kind, slot, e)
            with contextlib.suppress(Exception):
                sess.writer.close()
            return False


_server: GuestChannelServer | None = None


def server() -> GuestChannelServer | None:
    return _server


def _port() -> int:
    from awgbot.core import settings
    return int(settings.get_int("app.routing.link_channel_port", 8787) or 8787)


async def ensure(services) -> GuestChannelServer | None:
    """Слушатель — пока есть пара и стороны зоны; без них — снять."""
    global _server
    paired = await asyncio.to_thread(services.reserve_paired)
    links = await asyncio.to_thread(services.reserve_guest_links) if paired else {}
    if not paired or not links:
        if _server is not None:
            await _server.stop()
            _server = None
        return None
    if _server is None:
        _server = GuestChannelServer(services, _port())
    try:
        await _server.ensure()
    except Exception as e:                                # noqa: BLE001
        log.info("гостевой канал: слушатель не поднялся: %s", e)
    return _server


async def shutdown() -> None:
    global _server
    if _server is not None:
        await _server.stop()
        _server = None

