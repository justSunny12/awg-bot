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

from .common import MANDATORY, log


def setup_gateway_scheduler(services, bot):
    """Планировщик роли gateway: тик монитора, задачи канала линка, месячная
    копия и проверка обновлений — сетевые с джиттером.

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

    def _retry_gw_backup():
        scheduler.add_job(job_gw_backup, "date", run_date=timeutil.shift(timeutil.now(), 3600),
                          id="gw_backup_retry", replace_existing=True, max_instances=1, **MANDATORY)

    async def job_gw_backup():
        await pkg.monthly_backup(services, bot, "gw backup", on_undelivered=_retry_gw_backup)

    async def job_gw_backup_catchup():
        await pkg.monthly_backup(services, bot, "gw backup", catchup=True, on_undelivered=_retry_gw_backup)

    scheduler.add_job(job_gw_backup, pkg.backup_trigger(), id="gw_backup", max_instances=1, **MANDATORY)
    # E7: догон на старте, как у основного бота — устройство, выключенное 1-го
    # в 12:00, иначе теряло месячную копию (первый прогон только помечает
    # месяц, копии не делает). Через минуты, не сразу: линк и почта ещё поднимаются.
    scheduler.add_job(job_gw_backup_catchup, "date",
                      run_date=timeutil.shift(timeutil.now(), random.randint(60, 300)),
                      id="gw_backup_catchup", max_instances=1, **MANDATORY)

    def _gw_backup_hook(_key=None, _val=None):
        try:
            scheduler.reschedule_job("gw_backup", trigger=pkg.backup_trigger())
        except Exception as e:                            # noqa: BLE001
            log.warning("gw_backup reschedule: %s", e)
    for key in ("app.scheduler.backup_day", "app.scheduler.backup_hour"):
        settings.on_change(key, _gw_backup_hook)

    pkg.add_update_check(scheduler, services, bot, run_date=timeutil.now(), tag="gw update_check")
    scheduler.start()
    return scheduler
