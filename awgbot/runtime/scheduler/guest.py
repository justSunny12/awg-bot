"""guest.py — часть пакета scheduler (см. __init__): планировщик бота основы
на резерве (гостевой режим, setup_guest_scheduler).

Ровно задания данных клиентов (jobs.py: опрос трафика клона, сроки и паузы,
месячный сброс с догоном, очистка истории, опрос почты, имена аккаунтов) и
три своих: монитор без хоста (сверка блокировок в зоне и состояние клона),
шифрованный снимок реплики (такт 30 с по отметке крючка базы), такт
локального канала с ботом резерва (5 с: отметка «ставить вебхук», число
устройств на связи). Чего нет: SSH-доступа и портов sshd, метрик железа,
доставки шлюзам, ресурс-алертов, набора обновлений, сопряжений, окна переезда
профилей, бэкапов, проверки обновлений — всё это действует на хост основы."""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.runtime import scheduler as pkg          # подменяемое тестами — через пакет

from .common import MANDATORY, log

SNAPSHOT_TICK_SECONDS = 30
CTL_TICK_SECONDS = 5
# задания гостя — сторож-тест сверяет с планировщиком
GUEST_JOBS: tuple[str, ...] = ("poll", "expiry", "monthly", "monthly_catchup", "purge_history", "email_resume",
                               "tg_names", "tg_names_startup", "guest_monitor", "guest_snapshot", "guest_ctl")


def setup_guest_scheduler(services, bot, db, on_ctl_tick=None) -> AsyncIOScheduler:
    """Собирает планировщик гостя (не запущенный — start() в main).
    on_ctl_tick — корутина такта локального канала (runtime/main/guest.py)."""
    scheduler = AsyncIOScheduler(timezone=config.TZ)
    now = timeutil.now()
    _TZ = config.TZ
    core = pkg.core_jobs(services, bot, db)

    def _trig_poll():
        return IntervalTrigger(minutes=settings.get_int("app.scheduler.traffic_poll_minutes", 5), timezone=_TZ)

    def _trig_expiry():
        return IntervalTrigger(minutes=settings.get_int("app.scheduler.expiry_check_minutes", 60), timezone=_TZ)

    def _trig_monitor():
        mins = settings.get_int("app.scheduler.monitor_minutes", 3)
        return IntervalTrigger(minutes=mins, jitter=max(1, int(mins * 60 * 0.2)), timezone=_TZ)

    def _trig_monthly():
        return CronTrigger(day=settings.get_int("app.scheduler.monthly_reset_day", 1),
                           hour=settings.get_int("app.scheduler.monthly_reset_hour", 0), minute=0, timezone=_TZ)

    def _trig_purge():
        return CronTrigger(hour=settings.get_int("app.history.purge_hour", 3), minute=0, timezone=_TZ)

    def _trig_email():
        secs = max(60, settings.get_int("email.poll_interval_sec", 60))
        return IntervalTrigger(seconds=secs, jitter=max(5, int(secs * 0.3)), timezone=_TZ)

    # ── монитор без хоста: блокировки в зоне, состояние клона ───────────────
    async def job_guest_monitor():
        try:
            restarted = await asyncio.to_thread(services.detect_and_handle_restart)
            if restarted:
                log.info("гость: перезапуск — блокировки переналожены")
            else:
                await asyncio.to_thread(services.reconcile_blocks, True)
        except Exception as e:                       # noqa: BLE001
            log.warning("гость: сверка блокировок: %s", e)
        try:
            ok = await asyncio.to_thread(services.server_ok)
            await asyncio.to_thread(db.set_state, "last_server_ok", "1" if ok else "0")
        except Exception as e:                       # noqa: BLE001
            log.warning("гость: состояние клона: %s", e)

    async def job_guest_snapshot():
        try:
            if await asyncio.to_thread(services.guest_snapshot_tick):
                log.debug("гость: снимок записан")
        except Exception as e:                       # noqa: BLE001
            log.warning("гость: снимок: %s", e)

    async def job_guest_ctl():
        if on_ctl_tick is None:
            return
        try:
            await on_ctl_tick()
        except Exception as e:                       # noqa: BLE001
            log.warning("гость: такт канала: %s", e)

    scheduler.add_job(core["poll"], _trig_poll(), id="poll", max_instances=1, coalesce=True,
                      next_run_time=now, misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
    scheduler.add_job(core["expiry"], _trig_expiry(), id="expiry", max_instances=1, coalesce=True,
                      next_run_time=now, misfire_grace_time=config.MISFIRE_GRACE_EXPIRY_SECONDS)
    scheduler.add_job(core["monthly"], _trig_monthly(), id="monthly", max_instances=1, **MANDATORY)
    scheduler.add_job(core["monthly_catchup"], "date", run_date=now, id="monthly_catchup", **MANDATORY)
    scheduler.add_job(core["purge_history"], _trig_purge(), id="purge_history", max_instances=1,
                      misfire_grace_time=config.MISFIRE_GRACE_CRON_SECONDS)
    scheduler.add_job(core["email_resume"], _trig_email(), id="email_resume", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
    scheduler.add_job(core["tg_names"], CronTrigger(hour=4, minute=30, jitter=1800, timezone=_TZ),
                      id="tg_names", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_CRON_SECONDS)
    scheduler.add_job(core["tg_names"], "date", run_date=now, id="tg_names_startup",
                      max_instances=1, misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
    scheduler.add_job(job_guest_monitor, _trig_monitor(), id="guest_monitor", max_instances=1, coalesce=True,
                      next_run_time=now, misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
    scheduler.add_job(job_guest_snapshot, IntervalTrigger(seconds=SNAPSHOT_TICK_SECONDS, jitter=5, timezone=_TZ),
                      id="guest_snapshot", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
    scheduler.add_job(job_guest_ctl, IntervalTrigger(seconds=CTL_TICK_SECONDS, timezone=_TZ),
                      id="guest_ctl", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    def _make_reschedule(job_id, factory):
        def hook(key, value):
            try:
                scheduler.reschedule_job(job_id, trigger=factory())
            except Exception as e:                            # noqa: BLE001
                log.warning("reschedule '%s' (%s): %s", job_id, key, e)
        return hook

    for _key, (_job_id, _factory) in {
        "app.scheduler.traffic_poll_minutes": ("poll", _trig_poll),
        "app.scheduler.expiry_check_minutes": ("expiry", _trig_expiry),
        "app.scheduler.monitor_minutes": ("guest_monitor", _trig_monitor),
        "app.scheduler.monthly_reset_day": ("monthly", _trig_monthly),
        "app.scheduler.monthly_reset_hour": ("monthly", _trig_monthly),
        "app.history.purge_hour": ("purge_history", _trig_purge),
        "email.poll_interval_sec": ("email_resume", _trig_email),
    }.items():
        settings.on_change(_key, _make_reschedule(_job_id, _factory))
    return scheduler
