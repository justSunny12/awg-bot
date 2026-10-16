"""outbox.py — отправка: снимок и дельты, сборка фидов по частям, сервисы, ответы на доставки."""
from __future__ import annotations

import asyncio
import contextlib

from awgbot.domain import gwsnapshot
from awgbot.util import gwlink

from .common import SEND_TIMEOUT, log


class OutboxMixin:
    # ── отправка ─────────────────────────────────────────────────────────────

    async def _send(self, kind: str, body: dict | None = None, pad: int = 0) -> bool:
        writer = self._writer
        if writer is None:
            return False
        self._seq += 1
        line = gwlink.pack(self._channel_key(), kind, body, seq=self._seq, pad=pad, nonce=self._sn)
        try:
            writer.write(line)
            await asyncio.wait_for(writer.drain(), SEND_TIMEOUT)
            self._sent_bytes += len(line)
            self._io(tx=len(line))
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            why = f"не ушло за {SEND_TIMEOUT} с" if isinstance(e, asyncio.TimeoutError) else str(e)
            log.info("канал линка: отправка «%s» не прошла (%s)", kind, why)
            if isinstance(e, asyncio.TimeoutError):
                with contextlib.suppress(Exception):
                    writer.transport.abort()               # сессия мёртвая — рвём, бэкофф поднимет
            return False

    async def push(self, *, full: bool = False) -> bool:
        """Отправить снимок или дельту. Дельта пуста — не уходит НИЧЕГО: это
        обычное состояние канала на дни, а не особый случай. По одному:
        два push вперемешку (тик и poke) завершались в обратном порядке, и
        сервер держал устаревшее состояние до следующего тика."""
        async with self._push_lock:
            return await self._push(full=full)

    async def _push(self, *, full: bool = False) -> bool:
        if self._writer is None:
            return False
        try:
            snap = await asyncio.to_thread(self.services.gw_snapshot)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: снимок не собран: %s", e)
            return False
        if full or not self._prev:
            self._rev = 1
            self._prev = snap
            return await self._send("snap", {**snap, "rev": 1}, pad=gwlink.PAD_SNAP)
        patch = gwsnapshot.delta(self._prev, snap)
        if not patch:
            return False
        self._rev += 1
        self._prev = snap
        sent = await self._send("delta", {**patch, "rev": self._rev}, pad=gwlink.PAD_DELTA)
        if sent and "mark_status" in patch:
            # Пометка сменилась — не заставляем человека пересылать токен
            # руками, если канал уже жив.
            await self._maybe_claim()
        return sent

    def _collect_part(self, msg: dict) -> str | None:
        """Сложить часть фидов; вернуть целое, когда пришли все. Части другого
        отпечатка сбрасывают начатую сборку: сервер сменил фиды на ходу."""
        digest = str(msg.get("hash") or "")[:64]
        try:
            i, n = int(msg.get("i")), int(msg.get("n"))
        except (TypeError, ValueError):
            return None
        if not (0 < n <= gwlink.MAX_CHUNKS and 0 <= i < n):
            return None
        if self._parts.get("hash") != digest or self._parts.get("n") != n:
            self._parts = {"hash": digest, "n": n, "got": {}}
        self._parts["got"][i] = str(msg.get("z") or "")[:gwlink.CHUNK]
        if len(self._parts["got"]) < n:
            return None
        whole = "".join(self._parts["got"][k] for k in range(n))
        self._parts = {}
        return whole

    async def push_services(self) -> bool:
        """Сервисы этой сети — серверу: при
        подключении целиком, дальше — когда обзор нашёл изменение (services_changed)."""
        if self._writer is None:
            return False
        items = await asyncio.to_thread(self.services.services_local)
        return await self._send("svc", {"items": items}, pad=gwlink.PAD_SNAP)

    async def _send_result(self, kind: str, result: dict, digest: str | None = None) -> bool:
        """Ответ серверу на доставку (peer_svc_ack, own_ack): {ok, hash, n, error}."""
        return await self._send(kind, {"ok": bool(result.get("ok")),
                                       "hash": str(digest if digest is not None else result.get("hash") or "")[:64],
                                       "n": int(result.get("n") or 0),
                                       "error": str(result.get("error") or "")[:300]},
                                pad=gwlink.PAD_DELTA)
