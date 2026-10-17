"""backup.py — часть пакета scheduler (см. __init__): копия по расписанию — месячная,
догон на старте, повтор недоставленной. Тело месячной копии (monthly_backup) и
триггер общие для обеих ролей; задания сервера ставит add_server_backup, у агента
шлюза — свои в setup_gateway_scheduler."""

from __future__ import annotations

import asyncio

from apscheduler.triggers.cron import CronTrigger
from aiogram.types import FSInputFile

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.runtime import scheduler as pkg          # подменяемое тестами — через пакет

from .common import MANDATORY, log

BACKUP_JITTER = 1800           # до получаса позже к месячной копии (джиттер APScheduler — только вперёд)


async def monthly_backup(services, bot, log_tag: str, *, catchup: bool = False,
                         on_undelivered=None) -> None:
    """Автобэкап раз в месяц — одно тело на обе роли. Guard по «году-месяцу»:
    защита от двойного запуска и catch-up после даунтайма через границу месяца
    (задача дополнительно прогоняется на старте; catchup=True — только после
    назначенного момента месяца). Первый запуск (state пуст) только фиксирует
    месяц. Канал — почта, если задан и ушло; иначе файлами админу в чат.
    Не доставлено — on_undelivered() ставит повтор. Вся синхронщина — в
    потоке, event loop не ждёт ни БД, ни SMTP."""
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
    from awgbot.domain.backup.crypto import BackupKeyMissing
    try:
        try:
            paths = await asyncio.to_thread(services.make_backup)
        except BackupKeyMissing:
            # копия только шифрованная, а фразы нет (агент): молчать нельзя —
            # человек ждёт копию по расписанию; уведомление один раз за месяц
            await _backup_key_missing_notice(services, bot, db, ym, log_tag)
            return
        sent_by_mail = False
        if await asyncio.to_thread(services.backup_channel) == "email":
            try:
                await asyncio.to_thread(services.email_send_backup, paths)
                sent_by_mail = True
            except Exception as e:               # noqa: BLE001
                log.warning("%s: на почту не ушёл, шлю в Telegram: %s", log_tag, e)
        delivered = sent_by_mail
        if not sent_by_mail:
            for p in paths:
                try:
                    await bot.send_document(config.ADMIN_ID, FSInputFile(p))
                    delivered = True
                except Exception as e:               # noqa: BLE001
                    log.warning("%s: отправка %s: %s", log_tag, p, e)
        if delivered:
            await asyncio.to_thread(db.set_state, "last_backup", ym)
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
    from awgbot.bot import roles
    from awgbot.domain.services import Notification
    log.warning("%s: копия %s не сделана — фраза шифрования не задана", log_tag, ym)
    if (await asyncio.to_thread(db.get_state, BACKUP_NO_KEY_NOTICED_KEY) or "") >= ym:
        return
    br = roles.current()
    text = (f"⚠️ Плановый бэкап {br.host_gen} не сделан: фраза шифрования не задана, а {br.backup_keys}. "
            "Задай фразу в «💾 Бэкапы» → «🔐 Шифрование» — копия уйдёт при следующем запуске по расписанию")
    await send_notifications(bot, [Notification(config.ADMIN_ID, text)])
    await asyncio.to_thread(db.set_state, BACKUP_NO_KEY_NOTICED_KEY, ym)




def backup_trigger():
    """Месячная копия обеих ролей: день/час из настроек, джиттер — почта и файл
    в чат ровно в 12:00:00 первого числа были бы маячком."""
    return CronTrigger(day=settings.get_int("app.scheduler.backup_day", 1),
                       hour=settings.get_int("app.scheduler.backup_hour", 12),
                       minute=0, jitter=BACKUP_JITTER, timezone=config.TZ)


def add_server_backup(scheduler, services, bot, *, run_date) -> None:
    """Месячная копия сервера: по расписанию (backup_trigger, с джиттером),
    догон на старте (только после назначенного момента месяца), повтор через
    час, если собрана, но не доставлена; смена дня или часа из чата или
    руками в conf — перевешивание без рестарта. Задания обязательные
    (MANDATORY): без допуска по опозданию, висят в очереди, пока не выполнятся."""
    def _retry_backup():
        # копия собрана, но не доставлена: повтор через час, пока не уйдёт
        scheduler.add_job(job_backup, "date", run_date=timeutil.shift(timeutil.now(), 3600),
                          id="backup_retry", replace_existing=True, max_instances=1, **MANDATORY)

    async def job_backup():
        await pkg.monthly_backup(services, bot, "backup", on_undelivered=_retry_backup)

    async def job_backup_catchup():
        await pkg.monthly_backup(services, bot, "backup", catchup=True, on_undelivered=_retry_backup)

    scheduler.add_job(job_backup, pkg.backup_trigger(), id="backup", max_instances=1, **MANDATORY)
    scheduler.add_job(job_backup_catchup, "date", run_date=run_date, id="backup_catchup", **MANDATORY)

    def _hook(key, value):
        try:
            scheduler.reschedule_job("backup", trigger=pkg.backup_trigger())
            log.info("расписание 'backup' обновлено на лету (%s=%s)", key, value)
        except Exception as e:                            # noqa: BLE001
            log.warning("reschedule 'backup' (%s): %s", key, e)
    for key in ("app.scheduler.backup_day", "app.scheduler.backup_hour"):
        settings.on_change(key, _hook)
