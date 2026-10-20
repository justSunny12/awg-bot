"""client.py — сторона принявшего: подключение к адресу пригласившего в /30
линка пары, сессия сопряжения (hello{pair, eph} → pair_eph → код своему
админу → pair_ok → pair_admin → линк на новые ключи) и обычная сессия (hello →
st), переподключение с отступом и джиттером."""
from __future__ import annotations

import asyncio
import contextlib
import random

from awgbot.util import gwlink, pairproto

from .common import _BACKOFF, _Session, channel_key, close_session, log, send_line
from .inbound import InboundMixin


class PairClient(InboundMixin):
    def __init__(self, services):
        self.services = services
        self._task: asyncio.Task | None = None
        self._sess: _Session | None = None
        self._confirmed = False
        self._wake = asyncio.Event()
        self._dead = False

    @property
    def online(self) -> bool:
        return self._sess is not None and self._sess.hello

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._sess is not None:
            await close_session(self._sess)
            self._sess = None

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        attempt = 0
        while not self._dead:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:                        # noqa: BLE001
                log.info("канал пары: нет связи (%s)", e)
            if self._confirmed:
                attempt, self._confirmed = 0, False
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)] * random.uniform(0.6, 1.4)
            attempt += 1
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)
                attempt = 0

    async def _connect_once(self) -> None:
        target = await asyncio.to_thread(self.services.reserve_connect_address)
        secret = await asyncio.to_thread(self.services.reserve_channel_secret)
        if not target or not secret:
            raise OSError("адрес или ключ канала пары не определены")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target[0], target[1], limit=gwlink.MAX_LINE), timeout=20)
        gwlink.tcp_keepalive(writer)
        sess = _Session(writer, channel_key(secret), listener=False)
        pairing = await asyncio.to_thread(self.services.reserve_accept_hello_body)
        try:
            body = dict(pairing) if pairing else await asyncio.to_thread(self.services.reserve_hello_body)
            body["nonce"] = gwlink.nonce_b64(sess.sn)
            sess.sn_sent = True
            sess.seq += 1
            writer.write(gwlink.pack(sess.key, "hello", body, seq=sess.seq, pad=gwlink.PAD_DELTA))
            await writer.drain()
            first = await asyncio.wait_for(reader.readline(), 20)
            if not first:
                return
            msg = gwlink.unpack(sess.key, first, nonce=sess.sn)
            sess.cn = gwlink.nonce_from(msg.get("nonce"))
            last_seq = int(msg.get("seq") or 0)
            self._confirmed = True
            if pairing:
                sess.pairing = True
                await self._pairing_loop(reader, sess, msg, last_seq)
                return
            if msg.get("t") != "st":
                return
            sess.hello = True
            self._sess = sess
            await asyncio.to_thread(self.services.reserve_session_opened, {"caps": msg.get("caps") or []})
            await self._handle_safe(sess, msg)
            if not await send_line(sess, "st", await asyncio.to_thread(self.services.reserve_st_body)):
                return
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = gwlink.unpack(sess.key, line, last_seq=last_seq, nonce=sess.sn)
                last_seq = int(msg.get("seq") or 0)
                await asyncio.to_thread(self.services.reserve_last_word)
                if not await self._handle_safe(sess, msg):
                    break
        finally:
            if self._sess is sess:
                self._sess = None
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.services.reserve_session_closed)
            await close_session(sess)

    async def _pairing_loop(self, reader, sess: _Session, first: dict, last_seq: int) -> None:
        msg = first
        while True:
            kind = str(msg.get("t") or "")
            if kind == "pair_eph":
                body = pairproto.check(pairproto.PAIR_PROTO, kind, msg, pairproto.INVITER)
                res = await asyncio.to_thread(self.services.reserve_accept_eph_in, body)
                await self.event("code", await asyncio.to_thread(self.services.reserve_invite), code=res["code"])
            elif kind == "pair_wrong":
                await self.event("wrong", await asyncio.to_thread(self.services.reserve_invite),
                                 left=int(msg.get("left") or 0))
            elif kind == "pair_refused":
                why = str(msg.get("reason") or "gone")
                st = await asyncio.to_thread(self.services.reserve_invite)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.services.reserve_invite_cancel, why)
                await self.event(why, st)
                self._dead = True
                return
            elif kind == "pair_ok":
                reply = await asyncio.to_thread(self.services.reserve_accept_ok_in, msg)
                await send_line(sess, "pair_admin", reply)
                await self.event("linked", await asyncio.to_thread(self.services.reserve_peer))
                await self.notes(await asyncio.to_thread(self.services.reserve_linked_notes))
                self._confirmed = True
                return                                    # линк переходит на новые ключи — сессия заново
            else:
                log.warning("канал пары: в сопряжении пришло «%s» — разрыв", kind[:32])
                return
            line = await reader.readline()
            if not line:
                return
            msg = gwlink.unpack(sess.key, line, last_seq=last_seq, nonce=sess.sn)
            last_seq = int(msg.get("seq") or 0)

    async def send(self, kind: str, body: dict | None = None) -> bool:
        if self._sess is None or not self._sess.hello:
            return False
        return await send_line(self._sess, kind, body)
