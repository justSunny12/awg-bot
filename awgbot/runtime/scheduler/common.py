"""common.py — часть пакета scheduler (см. __init__): общее обеих ролей — уведомление о версии,
имена Telegram-аккаунтов, месячная копия, проверка обновлений, триггеры и MANDATORY."""

from __future__ import annotations

import asyncio
import datetime
import logging

from apscheduler.triggers.cron import CronTrigger
from aiogram.types import FSInputFile

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.runtime import scheduler as pkg          # подменяемое тестами — через пакет

log = logging.getLogger("awgbot.scheduler")


async def notify_update_available(bot, services, nxt) -> None:
    """Уведомление «доступна следующая версия» — общее для обеих ролей.

    Перед отправкой снимаем «В меню» у всех прошлых окон обновления: после
    ступени в чате висит финишер «обновлён до…» с живой кнопкой, а следующее
    сообщение — это уведомление; две живые кнопки в двух соседних окнах ведут
    в одно место. Тексты финишеров не трогаем.

    Хвост — молчащие шлюзы, чью версию агента цель уже не обслуживает
    (сверка версий ролей, без сети): обновиться можно, но о них надо знать."""
    from awgbot.bot import texts
    from awgbot.bot import keyboards as kb
    from awgbot.bot.notifier import send_notifications
    from awgbot.bot.handlers.common import dismiss_update_reports
    from awgbot.domain.services import Notification
    silent = None
    if hasattr(services, "update_silent_peers"):
        silent = await asyncio.to_thread(services.update_silent_peers, nxt)
    await dismiss_update_reports(bot, services)
    await send_notifications(bot, [Notification(
        config.ADMIN_ID, texts.update_available(nxt.tag, nxt.body, silent=silent,
                                                bot_username=getattr(services, "bot_username", "") or ""),
        reply_markup=kb.update_notify())])


TG_NAME_MAX_AGE_DAYS = 7      # имя аккаунта старше — переспросить у Telegram


async def refresh_tg_names(bot, db, max_age_days: int = TG_NAME_MAX_AGE_DAYS) -> int:
    """Имена Telegram-аккаунтов на профилях — для ссылок на людей в текстах.

    Кто пишет боту, у того имя обновляет middleware по каждому сообщению без
    единого запроса к API. Здесь — молчуны: у кого имени нет (после
    обновления — у всех) или оно старше max_age_days, спрашиваем get_chat —
    по одному, раз в сутки, десятки профилей. Экраны API не трогают никогда.
    Отказ API (заблокировал бота, удалил аккаунт) — молча, остаётся прежнее.
    Возвращает, скольким обновили."""
    cutoff = timeutil.to_iso(timeutil.now() - datetime.timedelta(days=max_age_days))
    rows = await asyncio.to_thread(db.clients_needing_tg_name, cutoff)
    done = 0
    for c in rows:
        try:
            chat = await bot.get_chat(c.tg_id)
        except Exception as e:                        # noqa: BLE001
            log.info("tg_name %s: %s", c.tg_id, e)
            continue
        name = (getattr(chat, "full_name", None) or " ".join(
            x for x in (getattr(chat, "first_name", "") or "", getattr(chat, "last_name", "") or "") if x)
            or getattr(chat, "username", "") or "").strip()[:128]
        if not name:
            continue
        uname = (getattr(chat, "username", "") or "").strip()[:64]
        await asyncio.to_thread(db.set_tg_identity, c.id, name, uname)
        done += 1
    return done


# Обязательные задания: без допуска по опозданию — APScheduler выполнит их,
# сколько бы они ни ждали (misfire_grace_time=None), а не выбросит как
# просроченные; накопившиеся запуски схлопываются в один.
MANDATORY = {"misfire_grace_time": None, "coalesce": True}


def planned_moment_passed(day: int, hour: int, ref=None) -> bool:
    """Назначенный момент этого месяца (день, час; день короче месяца —
    последний день) уже прошёл? Догон на старте работает только тогда: копия
    15-го не должна уходить 3-го после рестарта, а крон 15-го — пропускаться."""
    import calendar
    now = (ref or timeutil.now()).astimezone(config.TZ)
    last = calendar.monthrange(now.year, now.month)[1]
    planned = now.replace(day=max(1, min(int(day), last)), hour=max(0, min(int(hour), 23)),
                          minute=0, second=0, microsecond=0)
    return now >= planned


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
    if last == ym:
        return
    if catchup and not pkg.planned_moment_passed(settings.get_int("app.scheduler.backup_day", 1),
                                             settings.get_int("app.scheduler.backup_hour", 12)):
        return                                   # назначенный момент ещё впереди — крон своё сделает
    if last is None:
        await asyncio.to_thread(db.set_state, "last_backup", ym)   # первый запуск — только фиксация
        return
    try:
        paths = await asyncio.to_thread(services.make_backup)
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


_UPDATE_JITTER = 1800          # до получаса позже к проверке обновлений (джиттер APScheduler — только вперёд): не ровно в 10:00
BACKUP_JITTER = 1800           # до получаса позже к месячной копии — по той же причине


def update_check_trigger():
    """Триггер проверки обновлений по updates.poll_schedule (day|week|month).
    Время — poll_hour:minute, неизвестное расписание — как day; прежнее never
    планировщик читает как month (нормализует старт и раздел — домен,
    normalize_update_schedule): проверка идёт всегда ради «⬆️ Доступна vX».
    Одна фабрика на обе роли: у основного бота была своя копия без джиттера, и
    он ходил на GitHub ровно в назначенную секунду — маячок по расписанию."""
    sch = str(settings.get("updates.poll_schedule", "day")).lower()
    h = settings.get_int("updates.poll_hour", 10)
    m = settings.get_int("updates.poll_minute", 0)
    if sch == "never":
        sch = "month"
    if sch == "week":
        return CronTrigger(day_of_week=0, hour=h, minute=m, jitter=_UPDATE_JITTER, timezone=config.TZ)
    if sch == "month":
        return CronTrigger(day=1, hour=h, minute=m, jitter=_UPDATE_JITTER, timezone=config.TZ)
    return CronTrigger(hour=h, minute=m, jitter=_UPDATE_JITTER, timezone=config.TZ)


def update_check_hook(scheduler):
    """Хук settings.on_change для задачи update_check: перевесить на новый
    триггер (три ключа ведут сюда; расписания «никогда» нет). Модульная
    функция, а не замыкание, чтобы её можно было проверить фейковым
    планировщиком."""
    def _hook(key, value):
        try:
            scheduler.reschedule_job("update_check", trigger=pkg.update_check_trigger())
            log.info("расписание проверки обновлений обновлено (%s=%s)", key, value)
        except Exception as e:                            # noqa: BLE001
            log.warning("reschedule update_check (%s): %s", key, e)
    return _hook


def add_update_check(scheduler, services, bot, *, run_date, tag: str) -> None:
    """Проверка обновлений — одна на обе роли: по расписанию (update_check_trigger,
    с джиттером) и разово на старте (сразу после апдейта увидим следующую
    ступень, не дожидаясь утра); смена расписания из чата или руками в conf —
    перевешивание без рестарта. update_to_notify сам учитывает mute и «ровно
    один раз на версию». tag — префикс записи журнала."""
    async def job_update_check():
        try:
            found = await asyncio.to_thread(services.update_scan)   # шапка: «⬆️ Доступна vX»
            nxt = await asyncio.to_thread(services.update_to_notify, found)
            if nxt is not None:
                await pkg.notify_update_available(bot, services, nxt)
        except Exception as e:                        # noqa: BLE001
            log.warning("%s: %s", tag, e)

    scheduler.add_job(job_update_check, pkg.update_check_trigger(),
                      id="update_check", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_CRON_SECONDS)
    scheduler.add_job(job_update_check, "date", run_date=run_date, id="update_check_startup",
                      max_instances=1, misfire_grace_time=config.MISFIRE_GRACE_CRON_SECONDS)
    hook = pkg.update_check_hook(scheduler)
    for key in ("updates.poll_schedule", "updates.poll_hour", "updates.poll_minute"):
        settings.on_change(key, hook)


def backup_trigger():
    """Месячная копия обеих ролей: день/час из настроек, джиттер — почта и файл
    в чат ровно в 12:00:00 первого числа были бы маячком."""
    return CronTrigger(day=settings.get_int("app.scheduler.backup_day", 1),
                       hour=settings.get_int("app.scheduler.backup_hour", 12),
                       minute=0, jitter=BACKUP_JITTER, timezone=config.TZ)
