"""
pollctl.py — право отвечать в Telegram у основного бота: поллинг как
управляемая задача. Арбитр — вебхук Telegram: пока адрес
вебхука стоит у гостя, `getUpdates` отвечает 409 «webhook is
active». Здесь: на старте и перед каждым возобновлением — `getWebhookInfo`
(чужой адрес → не поллим, `rsv_poll = away`), а в ожидании права отвечать —
повторно раз в RECHECK_S ± 40 % или по `wake()` (вебхук пропал — поллинг сам); посредник запросов сессии
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
import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates
from awgbot.util import backoff

log = logging.getLogger(__name__)

POLL_TIMEOUT = 50
RECHECK_S = 300                   # в ожидании права отвечать — повторный getWebhookInfo
WEBHOOK_INFO_S = 10               # getWebhookInfo рычага и пробы пульта — не дольше


async def webhook_info(bot, why: str) -> dict | None:
    """getWebhookInfo без снятия вебхука (рычаг «вернуть без резерва», проба пульта): адрес,
    очередь, дата последней ошибки доставки в секундах эпохи; не ответил за WEBHOOK_INFO_S — None."""
    from awgbot.util import timeutil
    try:
        info = await asyncio.wait_for(bot.get_webhook_info(), WEBHOOK_INFO_S)
        return {"url": str(info.url or ""), "pending_update_count": int(info.pending_update_count or 0),
                "last_error_date": timeutil.epoch_seconds(getattr(info, "last_error_date", None))}
    except Exception as e:                                   # noqa: BLE001
        log.warning("резерв: %s: getWebhookInfo не прошёл: %s", why, e)
        return None


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
            # подтверждающий запрос release отдаёт следующее обновление (limit 1) — его
            # здесь никто не обработал: повторный release подтвердил бы его, и оно
            # потерялось бы и для резерва
            if updates and not self.ctl._confirming:
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
        self._confirming = False                             # идёт подтверждающий getUpdates release
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
        # решение «ждать права» принято здесь: wake() после этой точки (take() из такта,
        # увидевшего away, — ещё пока поллинг останавливается) не теряется
        self._wake.clear()
        log.warning("Telegram: вебхук у резерва — поллинг остановлен (%s)", why[:80])
        loop = asyncio.get_running_loop()
        loop.create_task(self._stop_after_conflict())

    async def _stop_after_conflict(self) -> None:
        await self._pult_handed()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.services.reserve_set_poll, "away", why="webhook")
        await self._halt()                                   # без clear: wake() после отметки away — в силе

    async def _pult_handed(self) -> None:
        """Вебхук снова у бота на резерве: пульт (domain reserve/pult) снят — по факту Telegram,
        а не по нажатию «📤 Отдать Telegram резерву»."""
        fn = getattr(self.services, "reserve_pult_webhook_back", None)
        if fn is None:
            return
        try:
            acts = await asyncio.to_thread(fn)
            if acts:
                from awgbot.runtime import rsvactions
                await rsvactions.run(acts)
        except Exception as e:                               # noqa: BLE001
            log.warning("пульт: отметка не снята: %s", e)

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
            # пульт — до отметки away; wake() от take() из такта, увидевшего away, не теряется:
            # run() снимает wake до проверки, а не перед ожиданием
            await self._pult_handed()
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
            # wake снимается ДО проверки, а не перед ожиданием: wake(), пришедший во время
            # getWebhookInfo или остановки поллинга, — повод перепроверить сразу, а не через RECHECK_S
            self._wake.clear()
            allowed = await self._allowed()
            if self._closing:
                break                                        # close() пришёл во время проверки (сетевой запрос) — поллинг не начинать
            if not allowed:
                with contextlib.suppress(asyncio.TimeoutError):
                    # право отвечать решает Telegram: перепроверять getWebhookInfo, а не ждать слова вечно
                    await asyncio.wait_for(self._wake.wait(), backoff.jitter(RECHECK_S))
                continue
            self._conflict = ""
            self._stopped = False
            self._task = asyncio.get_running_loop().create_task(
                self.dp.start_polling(self.bot, polling_timeout=POLL_TIMEOUT,
                                      allowed_updates=self.dp.resolve_used_update_types(), handle_signals=False))
            try:
                await self._task
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise                                    # отменён сам run() — наверх
                if self._closing:
                    return                                   # неначатый опрос снят в close() — выход, как после stop_polling
            except BaseException:
                self._task = None
                raise                                        # как прежде: сбой поллинга — наверх, в finally main
            self._task = None
            if self._closing or not (self._stopped or self._conflict):
                return                                       # поллинг кончился сам (сигнал, заглушка теста) — выход, как прежде
            # wake снят в момент решения остановиться (conflict / stop), не здесь: между
            # отметкой away и этой строкой — остановка поллинга, и wake() take() пропадал бы
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), backoff.jitter(RECHECK_S))

    def wake(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        """Остановить поллинг (ждёт завершения текущего getUpdates). Прежние wake()
        снимаются сейчас: будит только то, что пришло после решения остановиться."""
        if not self.polling():
            return
        self._wake.clear()
        await self._halt()

    async def _halt(self) -> None:
        if not self.polling():
            return
        self._stopped = True
        try:
            await self.dp.stop_polling()
        except RuntimeError:
            # поллинг ещё не начался (задача создана, start_polling не дошёл до цикла) —
            # останавливать нечего, а ждать задачу — вечно: снять её
            if self._task is not None:
                self._task.cancel()
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
        self._confirming = True
        try:
            await self.bot.get_updates(offset=self.last_update_id + 1, limit=1, timeout=0)
        except Exception as e:                               # noqa: BLE001
            log.warning("подтверждающий getUpdates не прошёл: %s", e)
            return False
        finally:
            self._confirming = False
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
