"""outbox.py — отправка: снимок и дельты, настройки с ВПС, итоги применения через перезапуск линка, сервисы."""
from __future__ import annotations

import asyncio
import contextlib

from awgbot.domain import gwsnapshot
from awgbot.util import gwlink
from awgbot.runtime import linkclient as pkg          # адрес, порт, крючок и задержки — на пакете: их подменяют тесты

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
            # Пометка сменилась (применили бандл нового слота) — не заставляем
            # человека пересылать токен руками, если канал уже жив.
            await self._maybe_claim()
        return sent

    async def _apply_settings(self, values) -> None:
        """Настройки с сервера: проверить, применить, ответить, сказать человеку.

        Применение — в потоке: рестарт юнита обвязки идёт секунды, и держать на
        это время цикл событий агента (его Telegram) незачем. Ответ `ack` несёт
        итог применения; что доставка завершена, сервер видит
        завершена по снимку, который уходит следом."""
        result = await asyncio.to_thread(self.services.apply_link_settings, values or {})
        body = {"ok": bool(result.get("ok")), "changed": list(result.get("changed") or []),
                "error": str(result.get("error") or "")[:300]}
        if result.get("retry"):
            # временный отказ (юнит ещё работает): пусть сервер повторит набор в
            # этой же сессии — с keepalive сессии живут днями, и «отправлено
            # один раз за сессию» превращало бы отказ в долгий
            body["retry"] = True
        await self._send("ack", body, pad=gwlink.PAD_DELTA)
        if (result.get("changed") or not result.get("ok")) and not result.get("retry"):
            # Человек узнаёт о факте в чате агента: применено без его кнопки,
            # значит сказать обязательно. Пустое применение — тишина; временный
            # отказ с повтором — тоже: сервер повторит, итог придёт один раз.
            if pkg._notify is not None:
                try:
                    await pkg._notify(self.services.link_settings_note(result))
                except Exception as e:                    # noqa: BLE001
                    log.info("канал линка: уведомление не ушло: %s", e)
            await self.push()                             # снимок с новыми значениями — сразу

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

    async def flush_applied(self) -> bool:
        """Отправить серверу отложенный итог применения файла. Из очереди он
        уходит по подтверждению сервера (applied_ack), а не по записи в сокет:
        бандл тут же перезапускает линк, и строка в буфере умершей сессии
        пропала бы вместе с ней. Повтор сервер отличает по отпечатку и времени."""
        if self._writer is None:
            return False
        pending = await asyncio.to_thread(self.services.applied_pending_get)
        if not pending:
            return False
        return await self._send("applied", {"ok": bool(pending.get("ok")), "error": pending.get("error") or "",
                                            "fp": pending.get("fp") or "", "at": pending.get("at") or ""},
                                pad=gwlink.PAD_DELTA)

    async def _applied_acked(self, msg: dict) -> None:
        """Сервер принял итог: очередь пуста. Подтверждение на другой файл
        (очередь успели перезаписать) очередь не трогает."""
        pending = await asyncio.to_thread(self.services.applied_pending_get)
        if not pending:
            return
        if str(pending.get("fp") or "") != str(msg.get("fp") or "") or \
                str(pending.get("at") or "") != str(msg.get("at") or ""):
            return
        await asyncio.to_thread(self.services.applied_pending_clear)

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
