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
            await self.notes(await asyncio.to_thread(self.services.reserve_pong_in, body["n"]))
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

    async def _in_pkg(self, sess: _Session, body: dict) -> None:
        """Часть пакета: складываем по поколению; целый — сервисам, ответ — основе."""
        gen, i, n = int(body["gen"]), int(body["i"]), int(body["n"])
        if not 0 < n <= pairproto.MAX_CHUNKS or not 0 <= i < n:
            raise pairproto.ProtocolError("часть пакета вне предела")
        for other in [k for k in sess.parts if not isinstance(k, tuple) and k != gen]:
            sess.parts.pop(other, None)                       # одна сборка пакета на сессию: прежнее поколение — прочь
        parts = sess.parts.setdefault(gen, [None] * n)
        if len(parts) != n:
            sess.parts[gen] = parts = [None] * n
        parts[i] = str(body["blob"])
        if any(p is None for p in parts):
            return
        sess.parts.pop(gen, None)
        try:
            blob = bytes.fromhex("".join(parts))
        except ValueError:
            await send_line(sess, "pkg_bad", {"gen": gen, "reason": "части не склеились"})
            return
        reply, why = await asyncio.to_thread(self.services.reserve_pkg_in, gen, str(body["fp"]), blob,
                                             str(body["share"]), int(body["m_gen"]))
        if reply == "pkg_ok":
            await send_line(sess, "pkg_ok", {"gen": gen, "fp": str(body["fp"])})
            await send_line(sess, "st", await asyncio.to_thread(self.services.reserve_st_body))
        else:
            await send_line(sess, "pkg_bad", {"gen": gen, "reason": why[:pairproto.MAX_REASON]})

    async def _actions(self, actions) -> None:
        from awgbot.runtime import rsvactions
        await rsvactions.run(actions)

    async def _in_go_req(self, sess: _Session, body: dict) -> None:
        why = await asyncio.to_thread(self.services.reserve_go_req_in, int(body["gen"]), str(body["kind"]),
                                      int(body.get("ttl") or 0))
        if why:
            await send_line(sess, "down", {"reason": why[:pairproto.MAX_REASON]})

    async def _in_open_eph(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_open_eph_in, int(body["gen"]),
                                                    str(body["eph"]), str(body["oid"])))

    async def _in_go(self, sess: _Session, body: dict) -> None:
        why = await asyncio.to_thread(self.services.reserve_go_in, str(body.get("k") or ""), int(body["gen"]),
                                      str(body["kind"]), int(body.get("ttl") or 0),
                                      str(body.get("sealed") or ""), str(body.get("oid") or ""))
        if why:
            # отказ конверту при ждущем процессе — срыв приёма (down уходит действием)
            await self._actions(await asyncio.to_thread(self.services.reserve_go_refused, why, bool(body.get("sealed"))))

    async def _in_up(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_up_in))

    async def _in_down(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_down_in, str(body["reason"])))

    async def _in_released(self, sess: _Session, body: dict) -> None:
        """Реплика частями: у основы (возврат) — слияние; у резерва (кнопка) — гостю."""
        gen, i, n = int(body["gen"]), int(body["i"]), int(body["n"])
        if not 0 < n <= pairproto.MAX_CHUNKS or not 0 <= i < n:
            raise pairproto.ProtocolError("часть реплики вне предела")
        key = ("released", gen)
        parts = sess.parts.setdefault(key, [None] * n)
        if len(parts) != n:
            sess.parts[key] = parts = [None] * n
        parts[i] = str(body["blob"])
        if any(p is None for p in parts):
            return
        sess.parts.pop(key, None)
        try:
            blob = bytes.fromhex("".join(parts))
        except ValueError:
            log.warning("канал пары: реплика не склеилась")
            return
        if await asyncio.to_thread(self.services.reserve_out) == "returning":
            await self._actions(await asyncio.to_thread(self.services.reserve_released_in, blob))
        else:
            await self._actions(await asyncio.to_thread(self.services.reserve_released_store, blob))

    async def _in_taken(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_taken_in))

    async def _in_back_req(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_back_req_in))

    async def _in_back(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_back_in))

    async def _in_relay(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_relay_in, str(body["until"])))

    async def _in_wiped(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_wiped_in))

    async def _in_evict(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_evict_in))

    async def _in_blocks(self, sess: _Session, body: dict) -> None:
        """Блокировки основы, пока её клиенты у меня, — гостю в зону."""
        await self._actions([("guest", "blocks", {"addrs": list(body.get("addrs") or [])[:4096]})])

    async def _in_slots(self, sess: _Session, body: dict) -> None:
        """Ключи шлюзовых сторон основы → стороны зоны, ответ slots_ok."""
        ok = await asyncio.to_thread(self.services.reserve_slots_in, list(body.get("slots") or []))
        await send_line(sess, "slots_ok", ok)

    async def _in_slots_ok(self, sess: _Session, body: dict) -> None:
        await asyncio.to_thread(self.services.reserve_slots_ok_in, list(body.get("slots") or []))

    async def _in_ask(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_ask_in, str(body["case"]), str(body["since"]),
                                                    list(body.get("voters") or [])))

    async def _in_ask_no(self, sess: _Session, body: dict) -> None:
        await self._actions(await asyncio.to_thread(self.services.reserve_ask_no_in, str(body["reason"])))

    async def _in_view(self, sess: _Session, body: dict) -> None:
        await self.notes(await asyncio.to_thread(self.services.reserve_view_in, str(body["case"]), str(body["since"]),
                                                 list(body.get("voters") or [])))

    async def _in_pkg_ok(self, sess: _Session, body: dict) -> None:
        await asyncio.to_thread(self.services.reserve_pkg_ok, int(body["gen"]), str(body["fp"]))

    async def _in_pkg_bad(self, sess: _Session, body: dict) -> None:
        await asyncio.to_thread(self.services.reserve_pkg_bad, int(body["gen"]), str(body["reason"]))

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
