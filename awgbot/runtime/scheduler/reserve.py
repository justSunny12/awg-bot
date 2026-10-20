"""reserve.py — задания резерва в планировщике основного бота: живость линка
пары (такт 5 с, локально, без сети: счётчик линка; проба по каналу — только
по подозрению), срок приглашения (раз в минуту, локально), подъём канала пары
по смене роли. Задания следующих этапов (пакет, голоса, надзор) — здесь же."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.triggers.interval import IntervalTrigger

from awgbot.core import config

log = logging.getLogger(__name__)


def setup_reserve_jobs(scheduler, services, bot) -> None:
    from awgbot.bot.notifier import send_notifications

    async def job_liveness():
        try:
            act, notes = await asyncio.to_thread(services.reserve_liveness_tick)
            if notes:
                await send_notifications(bot, notes)
            if act == "ping":
                from awgbot.runtime import pairlink
                await pairlink.send("ping", {"n": services.reserve_ping_nonce()})
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: такт живости: %s", e)

    scheduler.add_job(job_liveness, IntervalTrigger(seconds=5, timezone=config.TZ),
                      id="rsv_liveness", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    async def job_invite_tick():
        try:
            st = await asyncio.to_thread(services.reserve_invite_tick)
            if st:
                from awgbot.runtime import pairlink
                await pairlink.pair_close("expired", notify=True)
            from awgbot.runtime import pairlink
            await pairlink.ensure(services)
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: такт приглашения: %s", e)

    scheduler.add_job(job_invite_tick, IntervalTrigger(minutes=1, timezone=config.TZ),
                      id="rsv_invite_tick", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
