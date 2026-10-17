"""gateway.py — часть пакета scheduler (см. __init__): планировщик роли gateway (setup_gateway_scheduler)."""

from __future__ import annotations

import asyncio
import random

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.runtime import scheduler as pkg          # подменяемое тестами — через пакет

from .common import log


def setup_gateway_scheduler(services, bot):
    """Планировщик роли gateway: тик монитора, задачи канала линка и проверка
    обновлений — сетевые с джиттером. Своих копий у шлюза нет: его настройки —
    в бэкапе сервера AWG (копия настроек по каналу, linkclient/keep).

    Нарочно не переиспользует setup_scheduler: задачи основной роли шлюзу не
    нужны, а «фабрика с ветками по роли» хуже двух честных функций.
    """
    from awgbot.bot.notifier import send_notifications

    scheduler = AsyncIOScheduler(timezone=config.TZ)

    async def job_gw_monitor():
        try:
            notes = await asyncio.to_thread(services.monitor_tick)
            await send_notifications(bot, notes)
        except Exception as e:                           # noqa: BLE001
            log.warning("gw_monitor: %s", e)
        try:
            # Дельта снимка в канал — здесь, а не своим расписанием: у канала
            # его нет по замыслу. Ничего не изменилось — не уходит ничего.
            from awgbot.runtime import linkclient
            await linkclient.on_tick(services)
        except Exception as e:                           # noqa: BLE001
            log.warning("канал линка: дельта не ушла: %s", e)

    def _trig_gw_monitor():
        # тик ходит в сеть (проба наружу) — джиттер, чтобы не быть маячком
        mins = settings.get_int("app.gateway.monitor_minutes", 3)
        return IntervalTrigger(minutes=mins, jitter=max(1, int(mins * 60 * 0.4)),
                               timezone=config.TZ)

    scheduler.add_job(
        job_gw_monitor, _trig_gw_monitor(),
        id="gw_monitor", max_instances=1, coalesce=True,
        next_run_time=timeutil.now(),
        misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    def _gw_monitor_hook(_key=None, _val=None):
        try:
            scheduler.reschedule_job("gw_monitor", trigger=_trig_gw_monitor())
        except Exception as e:                            # noqa: BLE001
            log.warning("gw_monitor reschedule: %s", e)
    settings.on_change("app.gateway.monitor_minutes", _gw_monitor_hook)

    # Списки локальной сети без VPN: фиды через
    # аплинк, раз в 6 ч с джиттером (строго периодический запрос — маячок),
    # первый раз через 3 мин после старта. Без LAN_MODE в юните — холостая.
    async def job_gw_lan_lists():
        try:
            notes = await asyncio.to_thread(services.lan_lists_update)
            await send_notifications(bot, notes)
        except Exception as e:                           # noqa: BLE001
            log.warning("gw_lan_lists: %s", e)

    scheduler.add_job(
        job_gw_lan_lists, IntervalTrigger(hours=6, jitter=int(6 * 3600 * 0.4), timezone=config.TZ),
        id="gw_lan_lists", max_instances=1, coalesce=True,
        next_run_time=timeutil.shift(timeutil.now(), random.randint(120, 300)),
        misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    # Сервисы соседних сетей: обзор mDNS
    # своей сети раз в 15 мин с джиттером (запрос не покидает сегмент, но пусть
    # не совпадает с тиком монитора), первый — через 2–5 мин; изменился список —
    # уходит серверу каналом. Без доступа между подсетями в юните — холостая.
    async def job_gw_lan_services():
        try:
            from awgbot.runtime import linkclient
            await linkclient.services_changed(services)
        except Exception as e:                           # noqa: BLE001
            log.warning("gw_lan_services: %s", e)

    scheduler.add_job(
        job_gw_lan_services,
        IntervalTrigger(minutes=15, jitter=int(15 * 60 * 0.4), timezone=config.TZ),
        id="gw_lan_services", max_instances=1, coalesce=True,
        next_run_time=timeutil.shift(timeutil.now(), random.randint(120, 300)),
        misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    pkg.add_update_check(scheduler, services, bot, run_date=timeutil.now(), tag="gw update_check")
    scheduler.start()
    return scheduler
