"""inbound.py — разбор входящих обычной сессии канала пары (после сопряжения):
`st`, `ping`/`pong`, `gone`; остальное — по этапам (пакет, переезд, возврат)."""
from __future__ import annotations

import asyncio
import contextlib

from awgbot.util import pairproto

from .common import _Session, log, send_line


class InboundMixin:

    async def _handle_safe(self, sess: _Session, msg: dict) -> bool:
        try:
            await self._handle(sess, msg)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # noqa: BLE001
            log.warning("канал пары: сообщение %r не обработано: %s", str(msg.get("t"))[:32], e)
            return False

    def _sender_side(self, sess: _Session) -> str:
        return pairproto.ACCEPTOR if sess.listener else pairproto.INVITER

    async def _handle(self, sess: _Session, msg: dict) -> None:
        kind = str(msg.get("t") or "")
        body = pairproto.check(pairproto.PAIR_PROTO, kind, msg, self._sender_side(sess))
        if kind == "st":
            notes = await asyncio.to_thread(self.services.reserve_st_in, body)
            await self.notes(notes)
            return
        if kind == "ping":
            await send_line(sess, "pong", {"n": body["n"]})
            return
        if kind == "pong":
            await asyncio.to_thread(self.services.reserve_pong_in, body["n"])
            return
        if kind == "gone":
            notes = await asyncio.to_thread(self.services.reserve_peer_gone)
            await self.notes(notes)
            with contextlib.suppress(Exception):
                await self.stop()
            return
        handler = getattr(self, f"_in_{kind}", None)
        if handler is None:
            log.info("канал пары: сообщение «%s» — этот выпуск его не ведёт", kind)
            return
        await handler(sess, body)

    async def notes(self, notes) -> None:
        if not notes:
            return
        from awgbot.runtime import pairlink as pkg
        if pkg._on_notes is None:
            return
        try:
            await pkg._on_notes(list(notes))
        except Exception as e:                            # noqa: BLE001
            log.warning("канал пары: уведомления не ушли: %s", e)

    async def event(self, name: str, st: dict, **extra) -> None:
        """Событие сопряжения — в чат (правка живого сообщения) крючком main."""
        from awgbot.runtime import pairlink as pkg
        if pkg._on_event is None:
            return
        try:
            await pkg._on_event(name, st, extra)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал пары: событие «%s» не показано: %s", name, e)
