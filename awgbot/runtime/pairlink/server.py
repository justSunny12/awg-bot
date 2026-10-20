"""server.py — сторона пригласившего: слушатель на своём адресе в /30 линка
пары, сессия сопряжения (hello{pair, eph} → pair_eph → код → pair_ok →
pair_admin) и обычная сессия (hello → st)."""
from __future__ import annotations

import asyncio
import contextlib

from awgbot.util import gwlink

from .common import HELLO_TIMEOUT, _Session, channel_key, close_session, log, send_line
from .inbound import InboundMixin


class PairServer(InboundMixin):
    def __init__(self, services):
        self.services = services
        self._server: asyncio.AbstractServer | None = None
        self._bound: tuple[str, int] | None = None
        self._sess: _Session | None = None
        self._pair_sess: _Session | None = None

    # ── жизненный цикл ───────────────────────────────────────────────────────

    def _wanted(self) -> tuple[str, int] | None:
        want = self.services.reserve_listen_address()
        return tuple(want) if want else None

    async def ensure(self) -> None:
        want = self._wanted()
        if self._bound and self._bound != want:
            await self._close_server()
        if want and self._server is None:
            try:
                self._server = await asyncio.start_server(self._serve, want[0], want[1], limit=gwlink.MAX_LINE)
                self._bound = want
                log.info("канал пары: слушаю %s:%s", *want)
            except OSError as e:
                log.info("канал пары: слушатель %s:%s не поднят (%s)", want[0], want[1], e)

    async def _close_server(self) -> None:
        srv, self._server, self._bound = self._server, None, None
        for s in (self._sess, self._pair_sess):
            if s is not None:
                await close_session(s)
        self._sess = self._pair_sess = None
        if srv is not None:
            srv.close()
            with contextlib.suppress(Exception):
                await srv.wait_closed()

    async def stop(self) -> None:
        await self._close_server()

    @property
    def online(self) -> bool:
        return self._sess is not None and self._sess.hello

    # ── сессии ───────────────────────────────────────────────────────────────

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        secret = await asyncio.to_thread(self.services.reserve_channel_secret)
        if not secret:
            writer.close()
            return
        sess = _Session(writer, channel_key(secret), listener=True)
        gwlink.tcp_keepalive(writer)
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
            if not line:
                return
            msg = gwlink.unpack(sess.key, line)
            if msg.get("t") != "hello":
                return
            sess.cn = gwlink.nonce_from(msg.get("nonce"))
            last_seq = int(msg.get("seq") or 0)
            msg["_peer_addr"] = str(peer[0])
            if msg.get("pair"):
                await self._serve_pairing(reader, sess, msg, last_seq)
                return
            await self._serve_session(reader, sess, msg, last_seq)
        except (gwlink.ProtocolError, asyncio.TimeoutError, ValueError, ConnectionError, OSError) as e:
            log.info("канал пары: коннект с %s отвергнут (%s)", peer[0], e)
        finally:
            if self._sess is sess:
                self._sess = None
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.services.reserve_session_closed)
            if self._pair_sess is sess:
                self._pair_sess = None
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.services.reserve_pair_session_closed, sess.sid)
            await close_session(sess)

    async def _serve_pairing(self, reader, sess: _Session, hello: dict, last_seq: int) -> None:
        res = await asyncio.to_thread(self.services.reserve_pair_hello, hello)
        if not res.get("ok"):
            await send_line(sess, "pair_refused", {"reason": str(res.get("why") or "gone")})
            if res.get("why") == "expired":
                await self.pair_close("expired", notify=True)
            return
        old, self._pair_sess = self._pair_sess, sess
        sess.pairing, sess.sid = True, res["sid"]
        if old is not None and old is not sess:
            await close_session(old)
        if not await send_line(sess, "pair_eph", res["body"]):
            return
        await self.event(res["event"], res["st"])
        if res["st"].get("state") == "accepted":
            await self.pair_accepted()
        while True:
            line = await reader.readline()
            if not line:
                break
            msg = gwlink.unpack(sess.key, line, last_seq=last_seq, nonce=sess.sn)
            last_seq = int(msg.get("seq") or 0)
            if msg.get("t") == "pair_admin":
                await asyncio.to_thread(self.services.reserve_pair_admin_in, msg)
                await self.event("linked", await asyncio.to_thread(self.services.reserve_peer))
                await self.notes(await asyncio.to_thread(self.services.reserve_linked_notes))
                break
            log.warning("канал пары: сессия сопряжения прислала «%s» — разрыв", str(msg.get("t"))[:32])
            break

    async def _serve_session(self, reader, sess: _Session, hello: dict, last_seq: int) -> None:
        old, self._sess = self._sess, sess
        if old is not None and old is not sess:
            await close_session(old)
        sess.hello = True
        sess.caps = [str(c)[:16] for c in (hello.get("caps") or [])[:16]]
        await asyncio.to_thread(self.services.reserve_session_opened, hello)
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

    # ── события из чата ──────────────────────────────────────────────────────

    async def pair_accepted(self) -> None:
        sess = self._pair_sess
        if sess is None:
            return
        body = await asyncio.to_thread(self.services.reserve_pair_ok_body)
        if body:
            await send_line(sess, "pair_ok", body)

    async def pair_wrong(self, left: int) -> None:
        if self._pair_sess is not None:
            await send_line(self._pair_sess, "pair_wrong", {"left": int(left)})

    async def pair_close(self, why: str, notify: bool = False) -> None:
        sess, self._pair_sess = self._pair_sess, None
        if sess is not None:
            await send_line(sess, "pair_refused", {"reason": why})
            await close_session(sess)
        st = await asyncio.to_thread(self.services.reserve_invite)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.services.reserve_invite_cancel, why)
        if notify and st:
            await self.event(why, st)
        await self.ensure()

    async def send(self, kind: str, body: dict | None = None) -> bool:
        if self._sess is None or not self._sess.hello:
            return False
        return await send_line(self._sess, kind, body)
