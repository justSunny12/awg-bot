"""backup.py — часть пакета scheduler (см. __init__): копия сервера AWG по
расписанию — месячная, догон на старте, повтор недоставленной, — и по событию;
задания ставит add_server_backup. У шлюза копий нет.

Без ключевой фразы копий нет: месячная говорит об этом раз в месяц, по событию
не делается вовсе. Идёт смена ключей линка — копия ждёт её конца (повтор через
5 минут ±40 %): на диске .next/.prev, шлюз может быть уже на новых ключах."""

from __future__ import annotations

import asyncio
import random

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from aiogram.types import FSInputFile

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.runtime import scheduler as pkg          # подменяемое тестами — через пакет

from .common import MANDATORY, log

BACKUP_JITTER = 1800           # до получаса позже к месячной копии (джиттер APScheduler — только вперёд)
EVENT_MINUTES = 5              # такт копии по событию; джиттер 40 %
REKEY_WAIT_S = 300             # смена ключей в работе — повтор месячной через столько ±40 %


async def deliver(services, bot, paths, log_tag: str, caption: str = "") -> bool:
    """Почта, если выбрана и ушло; иначе файлами админу в чат. True — ушло."""
    if await asyncio.to_thread(services.backup_channel) == "email":
        try:
            if caption:
                await asyncio.to_thread(services.email_send_backup, paths, caption)
            else:
                await asyncio.to_thread(services.email_send_backup, paths)
            return True
        except Exception as e:               # noqa: BLE001
            log.warning("%s: на почту не ушёл, шлю в Telegram: %s", log_tag, e)
    delivered = False
    for p in paths:
        try:
            if caption:
                await bot.send_document(config.ADMIN_ID, FSInputFile(p), caption=caption)
            else:
                await bot.send_document(config.ADMIN_ID, FSInputFile(p))
            delivered = True
        except Exception as e:               # noqa: BLE001
            log.warning("%s: отправка %s: %s", log_tag, p, e)
    return delivered


async def monthly_backup(services, bot, log_tag: str, *, catchup: bool = False,
                         on_undelivered=None, on_busy=None) -> None:
    """Автобэкап сервера AWG раз в месяц. Guard по «году-месяцу»:
    защита от двойного запуска и catch-up после даунтайма через границу месяца
    (задача дополнительно прогоняется на старте; catchup=True — только после
    назначенного момента месяца). Первый запуск (state пуст) только фиксирует
    месяц. Канал — почта, если задан и ушло; иначе файлами админу в чат.
    Не доставлено — on_undelivered() ставит повтор; идёт смена ключей линка —
    on_busy() ставит повтор через минуты. Вся синхронщина — в потоке, event
    loop не ждёт ни БД, ни SMTP."""
    if not settings.get_bool("app.scheduler.backup_enabled", True):
        return
    db = services.db
    ym = timeutil.now().strftime("%Y-%m")
    last = await asyncio.to_thread(db.get_state, "last_backup")
    if last and last >= ym:                      # сделано; новее «сейчас» — пояс сменился на западный
        return
    if catchup and not pkg.planned_moment_passed(settings.get_int("app.scheduler.backup_day", 1),
                                             settings.get_int("app.scheduler.backup_hour", 12)):
        return                                   # назначенный момент ещё впереди — крон своё сделает
    if last is None:
        await asyncio.to_thread(db.set_state, "last_backup", ym)   # первый запуск — только фиксация
        return
    busy = getattr(services, "backup_rekey_busy", None)
    if busy is not None and await asyncio.to_thread(busy) is not None:
        log.info("%s: идёт смена ключей линка — копия после неё", log_tag)
        if on_busy is not None:
            on_busy()
        return
    from awgbot.domain.backup.crypto import BackupKeyMissing
    made_at = timeutil.to_iso(timeutil.now())
    try:
        try:
            paths = await asyncio.to_thread(services.make_backup)
        except BackupKeyMissing:
            # без фразы копий нет: молчать нельзя — человек ждёт копию по
            # расписанию; уведомление один раз за месяц
            await _backup_key_missing_notice(services, bot, db, ym, log_tag)
            return
        caption = ""
        if hasattr(services, "backup_caption"):
            caption = await asyncio.to_thread(services.backup_caption)
        if await deliver(services, bot, paths, log_tag, caption):
            await asyncio.to_thread(db.set_state, "last_backup", ym)
            if hasattr(services, "backup_event_covered"):
                await asyncio.to_thread(services.backup_event_covered, made_at)
        else:
            # архив собран, наружу не ушёл (Telegram через лежащий линк):
            # «сделан» не ставим — повтор через час, пока не уйдёт
            log.warning("%s: копия %s собрана, но не доставлена — повторю через час", log_tag, ym)
            if on_undelivered is not None:
                on_undelivered()
    except Exception as e:                       # noqa: BLE001
        log.warning("%s: %s", log_tag, e)


BACKUP_NO_KEY_NOTICED_KEY = "backup_nokey_noticed"      # «год-месяц» пропущенной копии, о которой сказали


async def _backup_key_missing_notice(services, bot, db, ym: str, log_tag: str) -> None:
    from awgbot.bot.notifier import send_notifications
    from awgbot.domain.services import Notification
    log.warning("%s: копия %s не сделана — фраза шифрования не задана", log_tag, ym)
    if (await asyncio.to_thread(db.get_state, BACKUP_NO_KEY_NOTICED_KEY) or "") >= ym:
        return
    from awgbot.domain.notices import backup as nb
    text = nb.missed_backup_notice(bool(await asyncio.to_thread(db.gateways)))
    await send_notifications(bot, [Notification(config.ADMIN_ID, text)])
    await asyncio.to_thread(db.set_state, BACKUP_NO_KEY_NOTICED_KEY, ym)


async def event_backup(services, bot) -> bool:
    """Копия по событию: отметка созрела (≥ 10 минут с последнего события,
    смены ключей нет) — снять, доставить, снять отметку. Месячную отметку
    last_backup не двигает. True — копия ушла."""
    due = await asyncio.to_thread(services.backup_event_take)
    if not due:
        return False
    from awgbot.domain.backup.crypto import BackupKeyMissing
    try:
        paths = await asyncio.to_thread(services.backup_event_paths, due)
    except BackupKeyMissing:
        return False
    caption = await asyncio.to_thread(services.backup_caption, due)
    if not await deliver(services, bot, paths, "backup event", caption):
        log.warning("backup event: копия по событию собрана, но не доставлена — повторю тактом")
        return False
    await asyncio.to_thread(services.backup_event_done, str(due.get("at") or ""))
    return True


def backup_trigger():
    """Месячная копия: день/час из настроек, джиттер — почта и файл
    в чат ровно в 12:00:00 первого числа были бы маячком."""
    return CronTrigger(day=settings.get_int("app.scheduler.backup_day", 1),
                       hour=settings.get_int("app.scheduler.backup_hour", 12),
                       minute=0, jitter=BACKUP_JITTER, timezone=config.TZ)


def add_server_backup(scheduler, services, bot, *, run_date) -> None:
    """Месячная копия сервера: по расписанию (backup_trigger, с джиттером),
    догон на старте (только после назначенного момента месяца), повтор через
    час, если собрана, но не доставлена, и через минуты, если идёт смена
    ключей линка; смена дня или часа из чата или руками в conf — перевешивание
    без рестарта. Задания обязательные (MANDATORY): без допуска по опозданию,
    висят в очереди, пока не выполнятся. И копия по событию — такт 5 минут с
    джиттером."""
    def _retry_backup():
        # копия собрана, но не доставлена: повтор через час, пока не уйдёт
        scheduler.add_job(job_backup, "date", run_date=timeutil.shift(timeutil.now(), 3600),
                          id="backup_retry", replace_existing=True, max_instances=1, **MANDATORY)

    def _busy_backup():
        # смена ключей линка в работе: ждём её конца, а не снимаем полуготовое
        delay = REKEY_WAIT_S * random.uniform(0.6, 1.4)
        scheduler.add_job(job_backup, "date", run_date=timeutil.shift(timeutil.now(), delay),
                          id="backup_retry", replace_existing=True, max_instances=1, **MANDATORY)

    async def job_backup():
        await pkg.monthly_backup(services, bot, "backup", on_undelivered=_retry_backup, on_busy=_busy_backup)

    async def job_backup_catchup():
        await pkg.monthly_backup(services, bot, "backup", catchup=True, on_undelivered=_retry_backup,
                                 on_busy=_busy_backup)

    async def job_backup_event():
        try:
            await pkg.event_backup(services, bot)
        except Exception as e:                            # noqa: BLE001
            log.warning("backup event: %s", e)

    scheduler.add_job(job_backup, pkg.backup_trigger(), id="backup", max_instances=1, **MANDATORY)
    scheduler.add_job(job_backup_catchup, "date", run_date=run_date, id="backup_catchup", **MANDATORY)
    scheduler.add_job(job_backup_event,
                      IntervalTrigger(minutes=EVENT_MINUTES, jitter=int(EVENT_MINUTES * 60 * 0.4),
                                      timezone=config.TZ),
                      id="backup_event", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    def _hook(key, value):
        try:
            scheduler.reschedule_job("backup", trigger=pkg.backup_trigger())
            log.info("расписание 'backup' обновлено на лету (%s=%s)", key, value)
        except Exception as e:                            # noqa: BLE001
            log.warning("reschedule 'backup' (%s): %s", key, e)
    for key in ("app.scheduler.backup_day", "app.scheduler.backup_hour"):
        settings.on_change(key, _hook)

    def _host_hook(_key, _value):
        # адрес сервера — в Endpoint шлюзов и в бэкапе: свежая копия
        try:
            services.backup_event_mark("host")
        except Exception as e:                            # noqa: BLE001
            log.warning("бэкап по смене адреса сервера не отмечен: %s", e)
    settings.on_change("app.network.server_host", _host_hook)
