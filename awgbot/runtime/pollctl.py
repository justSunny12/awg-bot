"""
pollctl.py — право отвечать в Telegram у основного бота: поллинг как
управляемая задача. Арбитр — вебхук Telegram: пока адрес
вебхука стоит у бота основы на резерве, `getUpdates` отвечает 409 «webhook is
active». Здесь: на старте и перед каждым возобновлением — `getWebhookInfo`
(чужой адрес → не поллим, `rsv_poll = away`); посредник запросов сессии
ловит у `GetUpdates` отказ 409 и останавливает поллинг за один запрос
(aiogram сам повторял бы с отступом); `release()` — остановить поллинг и
подтвердить последнюю пачку (`getUpdates(offset = последний + 1, limit 1,
timeout 0)`) перед финальной репликой; `take()` — `deleteWebhook` без
сброса накопленного и поллинг снова. Без пары поведение прежнее: один
`start_polling` до остановки.
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates

log = logging.getLogger(__name__)

POLL_TIMEOUT = 50
RECHECK_S = 300                   # в ожидании права отвечать — повторный getWebhookInfo


class _Watch(BaseRequestMiddleware):
    """Посредник запросов: последний id обновления и отказ 409 на getUpdates."""

    def __init__(self, ctl: "PollControl"):
        self.ctl = ctl

    async def __call__(self, make_request, bot, method):
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        try:
            resp = await make_request(bot, method)
        except TelegramConflictError as e:
            if "webhook" in str(e).lower():
                self.ctl.conflict(str(e))
            raise
        try:
            # make_request отдаёт уже разобранный результат (список Update); у
            # прежних выпусков — Response с полем result
            updates = list(getattr(resp, "result", resp) or [])
            if updates:
                self.ctl.last_update_id = max(int(u.update_id) for u in updates)
        except Exception:                                    # noqa: BLE001
            pass
        return resp


class PollControl:
    def __init__(self, dp, bot, services):
        self.dp, self.bot, self.services = dp, bot, services
        self.last_update_id: int = 0
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._conflict = ""
        self._stopped = False                                # поллинг остановлен нами (release/конфликт), не сам
        mw = getattr(getattr(bot, "session", None), "middleware", None)
        if mw is not None and hasattr(mw, "register"):
            mw.register(_Watch(self))                     # заглушки тестов сессии не несут

    # ── состояние ───────────────────────────────────────────────────────────
    def polling(self) -> bool:
        return self._task is not None and not self._task.done()

    def conflict(self, why: str) -> None:
        """Из посредника: вебхук стоит у второй стороны — остановить поллинг."""
        if self._conflict:
            return
        self._conflict = why
        log.warning("Telegram: вебхук у резерва — поллинг остановлен (%s)", why[:80])
        loop = asyncio.get_running_loop()
        loop.create_task(self._stop_after_conflict())

    async def _stop_after_conflict(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.services.reserve_set_poll, "away", why="webhook")
        await self.stop()

    async def _allowed(self) -> bool:
        """Можно ли поллить: без пары — да; с парой — если вебхука нет."""
        try:
            paired = await asyncio.to_thread(self.services.reserve_paired)
        except Exception:                                    # noqa: BLE001
            paired = False
        if not paired:
            return True
        try:
            info = await self.bot.get_webhook_info()
        except Exception as e:                               # noqa: BLE001
            log.warning("getWebhookInfo не прошёл (%s) — поллю по прежнему состоянию", e)
            return await asyncio.to_thread(self.services.reserve_poll) == "mine"
        if info.url:
            await asyncio.to_thread(self.services.reserve_set_poll, "away", why="webhook_info", url=str(info.url)[:120])
            log.warning("Telegram: вебхук стоит на %s — право отвечать не у меня", info.url)
            return False
        await asyncio.to_thread(self.services.reserve_set_poll, "mine")
        return True

    # ── цикл ────────────────────────────────────────────────────────────────
    async def run(self) -> None:
        """Вместо `dp.start_polling`: поллинг, пока разрешено; ждать `wake()`.
        Сигналы остановки — свои: aiogram ставит их только внутри start_polling,
        а в ожидании права отвечать поллинга нет."""
        import signal
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, lambda: loop.create_task(self.close()))
        while not self._closing:
            if not await self._allowed():
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    # право отвечать решает Telegram: перепроверять getWebhookInfo, а не ждать слова вечно
                    await asyncio.wait_for(self._wake.wait(), RECHECK_S * random.uniform(0.6, 1.4))
                continue
            self._conflict = ""
            self._stopped = False
            self._task = asyncio.get_running_loop().create_task(
                self.dp.start_polling(self.bot, polling_timeout=POLL_TIMEOUT,
                                      allowed_updates=self.dp.resolve_used_update_types(), handle_signals=False))
            try:
                await self._task
            except asyncio.CancelledError:
                if self._closing:
                    raise
            except BaseException:
                self._task = None
                raise                                        # как прежде: сбой поллинга — наверх, в finally main
            self._task = None
            if self._closing or not (self._stopped or self._conflict):
                return                                       # поллинг кончился сам (сигнал, заглушка теста) — выход, как прежде
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), RECHECK_S * random.uniform(0.6, 1.4))

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        """Остановить поллинг (ждёт завершения текущего getUpdates)."""
        if not self.polling():
            return
        self._stopped = True
        try:
            await self.dp.stop_polling()
        except RuntimeError:
            pass
        with contextlib.suppress(asyncio.CancelledError, Exception):
            if self._task is not None:
                await self._task

    async def close(self) -> None:
        self._closing = True
        await self.stop()
        self._wake.set()

    # ── передача и возврат права ────────────────────────────────────────────
    async def release(self) -> bool:
        """Остановить поллинг и подтвердить последнюю пачку: обновления до
        `last_update_id` включительно у Telegram закрыты, дублей на резерве нет."""
        await self.stop()
        try:
            await self.bot.get_updates(offset=self.last_update_id + 1, limit=1, timeout=0)
        except Exception as e:                               # noqa: BLE001
            log.warning("подтверждающий getUpdates не прошёл: %s", e)
            return False
        return True

    async def take(self) -> bool:
        """`deleteWebhook` без сброса очереди и поллинг снова."""
        try:
            await self.bot.delete_webhook(drop_pending_updates=False)
        except Exception as e:                               # noqa: BLE001
            log.warning("deleteWebhook не прошёл: %s", e)
            return False
        await asyncio.to_thread(self.services.reserve_set_poll, "mine")
        self._conflict = ""
        self.wake()
        return True
