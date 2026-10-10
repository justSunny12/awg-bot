"""feeds.py — свои списки, общие для всех шлюзов, записи соседей, фиды локальной сети, installed и claim."""
from __future__ import annotations

import asyncio
import contextlib

from awgbot.domain import gwownlists, gwservices
from awgbot.util import gwlink

from awgbot.runtime import linkclient as pkg          # крючки уведомления и вопроса — на пакете
from .common import log


class FeedsMixin:
    # ── свои списки, общие для всех шлюзов ──

    async def push_own(self) -> bool:
        """Свои правки (pending) — серверу пачками по MAX_EVENTS; пусто — ничего."""
        if self._writer is None:
            return False
        run, events = await asyncio.to_thread(self.services.own_pending_events)
        if not events:
            return False
        sent = False
        for i in range(0, len(events), gwownlists.MAX_EVENTS):
            if not await self._send("own_ev", {"run": run, "ev": events[i:i + gwownlists.MAX_EVENTS]},
                                    pad=gwlink.PAD_SNAP):
                return sent
            sent = True
        return sent

    async def _apply_own(self, msg: dict) -> None:
        """Канон от сервера: применить и отчитаться; пропущен по правилам
        сверки — вместо ack уходят свои правки, следующий канон придёт с ними."""
        async with self._own_lock:
            result = await asyncio.to_thread(self.services.apply_own_lists, msg)
        if result.get("skipped"):
            await self.push_own()
            return
        await self._send_result("own_ack", result)
        self._schedule_fill(result)

    def _schedule_fill(self, result: dict) -> None:
        """Канон применён и подтверждён — адреса новых доменов «в туннель» в
        набор фоновой задачей: dig по каждому (до 500) не должен держать ни
        канал, ни блокировку списков. Итог — в журнал; следующий fill ждёт
        предыдущего (тот же файл)."""
        path = result.get("fill")
        fill = self.services.own_fill
        if not path:
            return
        prev = self._fill_task

        async def _run() -> None:
            if prev is not None and not prev.done():
                with contextlib.suppress(Exception):
                    await prev
            ok, tail = await asyncio.to_thread(fill, path)
            if ok:
                log.info("свои списки: %s", tail or "набор пополнен")
            else:
                log.warning("свои списки: набор не пополнен: %s", tail)
        self._fill_task = asyncio.create_task(_run())

    async def own_tick(self) -> None:
        """После тика монитора: сверка файлов (правка мимо агента) → правки
        серверу; отложенный канон (обвязка обновилась) → применить."""
        reconcile = self.services.own_reconcile
        # сверка — и без канала: правка мимо агента должна лечь в pending сразу,
        # чтобы панель честно показала «ждут синхронизации», а не «синхронизированы»
        async with self._own_lock:
            changed = await asyncio.to_thread(reconcile)
        if self._writer is None:
            return
        if changed or await asyncio.to_thread(self.services.own_unsent):
            await self.push_own()
        async with self._own_lock:
            result = await asyncio.to_thread(self.services.own_retry)
        if result:
            await self._send_result("own_ack", result)
            self._schedule_fill(result)
        # поломку на шлюзе починили — просим канон снова: own_ev без правок
        # сбрасывает у сервера «уже слал в этой сессии»
        if await asyncio.to_thread(self.services.own_nudge_due):
            run, _events = await asyncio.to_thread(self.services.own_pending_events)
            await self._send("own_ev", {"run": run or "-", "ev": []}, pad=gwlink.PAD_DELTA)

    async def _apply_peer_services(self, msg: dict) -> None:
        digest = str(msg.get("hash") or "")[:64]
        items = msg.get("items") if isinstance(msg.get("items"), list) else []
        result = await asyncio.to_thread(self.services.apply_peer_services, digest,
                                         items[:gwservices.MAX_PEER * 2])
        await self._send_result("peer_svc_ack", result, digest)

    async def retry_peer_services(self) -> bool:
        """Отложенное применение записей соседей (помощника не было): помощник
        появился — применить и отчитаться серверу тем же отпечатком."""
        if self._writer is None:
            return False
        result = await asyncio.to_thread(self.services.services_retry)
        if result:
            return await self._send_result("peer_svc_ack", result)
        # поломку на шлюзе починили — свой список серверу заново: по нему он
        # повторит записи соседей, если прошлый ответ был отказом
        if await asyncio.to_thread(self.services.services_nudge_due):
            return await self.push_services()
        return False

    async def _apply_lists(self, digest: str, z: str) -> None:
        result = await asyncio.to_thread(self.services.apply_lan_feeds, digest, z)
        await self._send("lists_ack", {"ok": bool(result.get("ok")), "hash": digest,
                                       "error": str(result.get("error") or "")[:300]},
                         pad=gwlink.PAD_DELTA)

    async def _maybe_installed(self) -> None:
        """Первый выход на связь после установки — серверу «installed»: там по
        нему уберут файл первого применения из чата и скажут админу, что шлюз
        настроен. После снимка: серверу нужен бот шлюза из него."""
        if not await asyncio.to_thread(self.services.installed_report_pending):
            return
        if await self._send("installed", {}, pad=gwlink.PAD_DELTA):
            await asyncio.to_thread(self.services.installed_report_done)

    async def _maybe_claim(self) -> None:
        """Шлюз в основном боте не помечен — отправить токен пометки каналом.
        Проверка на той стороне — общая с ручной пересылкой, включая нонсы."""
        try:
            token = await asyncio.to_thread(self.services.gateway_claim_if_needed)
        except Exception as e:                            # noqa: BLE001
            log.info("канал линка: claim не собран: %s", e)
            return
        if token:
            await self._send("claim", {"token": token}, pad=gwlink.PAD_SNAP)

    # ── документ конфигурации ──

    async def _apply_cfg(self, msg: dict) -> None:
        """Документ с сервера: проверить, применить по разделам, ответить cfg_ack,
        сказать человеку; вопросы о почте и фразе — живым меню.

        Применение — в потоке (рестарт юнита и линка идут секунды). Ответ
        сначала в очередь: транзакция могла перезапустить линк, и сессия, по
        которой пришёл документ, умерла — тогда ответ уйдёт первым сообщением
        новой; ушёл сейчас — очередь пуста."""
        result = await asyncio.to_thread(self.services.apply_cfg, msg)
        ack = result["ack"]
        await asyncio.to_thread(self.services.cfg_ack_pending_put, ack)
        await self.flush_cfg_ack()
        if result.get("note") and pkg._notify is not None:
            try:
                await pkg._notify(result["note"])
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: уведомление о документе не ушло: %s", e)
        for section in result.get("ask") or []:
            if pkg._ask is not None:
                try:
                    await pkg._ask(section)
                except Exception as e:                    # noqa: BLE001
                    log.warning("канал линка: вопрос о разделе %s не показан: %s", section, e)

    async def flush_cfg_ack(self) -> bool:
        """Отправить отложенный cfg_ack; ушёл — очередь пуста."""
        if self._writer is None:
            return False
        pending = await asyncio.to_thread(self.services.cfg_ack_pending_get)
        if not pending:
            return False
        body = {k: v for k, v in pending.items() if k != "at"}
        if not await self._send("cfg_ack", body, pad=gwlink.PAD_DELTA):
            return False
        await asyncio.to_thread(self.services.cfg_ack_pending_clear)
        return True

