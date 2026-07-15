"""
scheduler.py — фоновые задачи (APScheduler 3.x, AsyncIOScheduler).

Задачи из ТЗ 11:
  • опрос трафика            — каждые TRAFFIC_POLL_MINUTES
  • проверка сроков          — каждые EXPIRY_CHECK_MINUTES + рассылка порогов
  • сброс месячного трафика  — 1-го числа 00:00 UTC+3 (с защитой от двойного)
  • автобэкап                — 1-го числа 12:00 UTC+3 (админу файлами)
  • мониторинг               — каждые MONITOR_MINUTES: детект рестарта →
                               реконсиляция блокировок, ребайнд вотчдога,
                               уведомление админа о смене статуса

services синхронны → зовём через asyncio.to_thread, чтобы не морозить loop.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from aiogram.types import FSInputFile

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot.notifier import send_notifications
from awgbot.domain.services import Notification

log = logging.getLogger("awgbot.scheduler")


def _service_failure_alerts(db, ok: bool) -> list:
    """Громкий алерт при НЕПРЕРЫВНОМ простое awg-сервиса ≥ N минут.
    Состояние простоя держим в state-таблице (переживает рестарт бота):
    service_down_since — ISO начала текущего простоя; service_alert_sent — «1»,
    если за этот эпизод громкий алерт уже отправлен. Мигнул вверх → сброс.
    Возвращает список Notification (0 или 1)."""
    if ok:
        db.set_state("service_down_since", "")
        db.set_state("service_alert_sent", "")
        return []
    since = db.get_state("service_down_since")
    if not since:
        db.set_state("service_down_since", timeutil.to_iso(timeutil.now()))
        return []
    down_secs = (timeutil.now() - timeutil.parse_iso(since)).total_seconds()
    if (down_secs >= settings.get_int("app.monitoring.service_failure_alert_minutes", 5) * 60
            and db.get_state("service_alert_sent") != "1"):
        db.set_state("service_alert_sent", "1")
        if settings.get_bool("app.monitoring.service_failure_alert_loud", True):
            mins = settings.get_int("app.monitoring.service_failure_alert_minutes", 5)
            return [Notification(
                config.ADMIN_ID,
                f"🚨 VPN-сервис не поднимается уже более {mins} мин. "
                "Требуется вмешательство.",
                force_sound=True)]
    return []


def setup_scheduler(services, bot, db, watcher=None) -> AsyncIOScheduler:
    """Собирает и возвращает планировщик (не запущенный — start() в main)."""
    scheduler = AsyncIOScheduler(timezone=config.TZ)

    # ── опрос трафика ────────────────────────────────────────────────────────
    async def job_poll():
        try:
            await asyncio.to_thread(services.poll_traffic)
            notifs = await asyncio.to_thread(services.check_traffic_limits)
            await send_notifications(bot, notifs)
        except Exception as e:                       # noqa: BLE001
            log.warning("poll_traffic: %s", e)

    # ── проверка сроков + уведомления ────────────────────────────────────────
    async def job_expiry():
        try:
            notifs = await asyncio.to_thread(services.check_expiry)
            # прикрепить кнопку отсрочки к отмеченным уведомлениям (UI-слой —
            # scheduler-композиция, чтобы services не тянул keyboards)
            from awgbot.bot import keyboards as kb
            for n in notifs:
                if getattr(n, "grace_offer_client_id", 0):
                    n.reply_markup = kb.grace_offer(n.grace_offer_client_id, settings.get_int("grace.grace_days", 14))
            await send_notifications(bot, notifs)
            # авто-выход из приостановок по истечении зарезервированного срока
            pause_notes = await asyncio.to_thread(services.check_pauses)
            await send_notifications(bot, pause_notes)
        except Exception as e:                       # noqa: BLE001
            log.warning("check_expiry: %s", e)

    # ── сброс месячного трафика ──────────────────────────────────────────────
    # Guard по «году-месяцу» даёт и защиту от двойного запуска, и catch-up:
    # задача дополнительно прогоняется на старте (date-триггер ниже), поэтому
    # пропущенный из-за даунтайма cron 1-го числа навёрстывается при первом же
    # запуске в новом месяце. Первый запуск бота (state пуст) сброс не делает —
    # нечего сбрасывать, просто фиксирует текущий месяц.
    async def job_monthly():
        ym = timeutil.now().strftime("%Y-%m")
        last = db.get_state("last_monthly_reset")
        if last == ym:
            return
        if last is None:
            db.set_state("last_monthly_reset", ym)       # первый запуск — только фиксация
            return
        try:
            reset_notes = await asyncio.to_thread(services.reset_monthly_traffic)
            db.set_state("last_monthly_reset", ym)
            await send_notifications(bot, reset_notes)
            log.info("Месячный трафик сброшен (catch-up или cron) за %s", ym)
        except Exception as e:                       # noqa: BLE001
            log.warning("monthly_reset: %s", e)

    # ── автобэкап (та же catch-up-схема) ─────────────────────────────────────
    async def job_backup():
        ym = timeutil.now().strftime("%Y-%m")
        last = db.get_state("last_backup")
        if last == ym:
            return
        if last is None:
            db.set_state("last_backup", ym)              # первый запуск — только фиксация
            return
        try:
            paths = await asyncio.to_thread(services.make_backup)
            for p in paths:
                try:
                    await bot.send_document(config.ADMIN_ID, FSInputFile(p))
                except Exception as e:               # noqa: BLE001
                    log.warning("Отправка бэкапа %s: %s", p, e)
            db.set_state("last_backup", ym)
        except Exception as e:                       # noqa: BLE001
            log.warning("backup: %s", e)

    async def job_purge_history():
        """Ежедневно: удалить историю старше ретеншна (батчами). Идемпотентно —
        если чистить нечего, просто отработает вхолостую."""
        try:
            removed = await asyncio.to_thread(services.purge_old_history)
            if removed:
                log.info("Очистка истории: удалено %s", removed)
        except Exception as e:                       # noqa: BLE001
            log.warning("purge_history: %s", e)

    # ── мониторинг: рестарт + статус + локальные метрики железа ──────────────
    async def job_monitor():
        try:
            restarted = await asyncio.to_thread(services.detect_and_handle_restart)
            if restarted:
                log.info("Обнаружен рестарт контейнера — блокировки переналожены")
            # пер-пирный SSH-к-хосту: реассерт каждый тик — дёшево и гарантирует
            # сходимость (удаление админ-устройства, переиспользование его IP,
            # рестарт контейнера уберут/вернут правила в пределах одного цикла).
            try:
                await asyncio.to_thread(services.reconcile_ssh_access)
            except Exception as e:                       # noqa: BLE001
                log.warning("reconcile_ssh_access: %s", e)
            # ребайнд вотчдога: PID меняется ТОЛЬКО при рестарте (который мы
            # детектим по StartedAt), поэтому дёргать container_pid каждый тик
            # незачем — только при рестарте или если наблюдатель умер.
            if watcher is not None and (restarted or not watcher.alive()):
                watcher.ensure_watching()
            # статус сервиса awg → уведомления админу (единый notifier-путь)
            ok = await asyncio.to_thread(services.server_ok)
            prev = db.get_state("last_server_ok")
            cur = "1" if ok else "0"
            alert_notes = []
            # (1) скачок статуса 🔴/🟢 — обычное уведомление (тихое ночью)
            if prev is not None and prev != cur:
                from awgbot.bot import texts
                alert_notes.append(Notification(
                    config.ADMIN_ID, texts.HB_SERVER_UP if ok else texts.HB_SERVER_DOWN))
            db.set_state("last_server_ok", cur)
            # (2) устойчивый простой сервиса ≥ N минут → ГРОМКИЙ алерт (один раз)
            alert_notes += _service_failure_alerts(db, ok)
            # (3) метрики железа: co-located — читаем локально (/proc, statvfs),
            #     снимок в state (инфобокс) + гистерезис ресурс-алертов
            from awgbot.runtime import hostmetrics
            snap = await asyncio.to_thread(hostmetrics.collect_and_store, db)
            alert_notes += services.check_resource_alerts(snap)
            await send_notifications(bot, alert_notes)
        except Exception as e:                       # noqa: BLE001
            log.warning("monitor: %s", e)

    # Интервальные задачи стартуют СРАЗУ (next_run_time=now), а не через первый
    # интервал: иначе бот в крэш-лупе с рестартом чаще часа никогда не проверил
    # бы сроки. Monthly/backup дополнительно прогоняются один раз на старте
    # (catch-up после даунтайма через границу месяца).
    # misfire_grace_time даёт запас: короткая задержка старта (подключение
    # вотчдога и т.п.) не должна съедать первый запуск задачи.
    # timezone=config.TZ и в интервальных триггерах — чтобы всё жило в UTC+3,
    # а не в системной зоне сервера (у нас Europe/London).
    now = timeutil.now()

    # ── Класс 2: расписания/интервалы читаем из settings ФАБРИКАМИ. Одна фабрика
    #    строит триггер и при первичной регистрации, и в on-change хуке ниже
    #    (reschedule_job) — единый источник, смена в conf/*.yaml применяется на
    #    лету без рестарта. Ключи → job_id для перевешивания в _CLASS2.
    _TZ = config.TZ

    def _trig_poll():
        return IntervalTrigger(minutes=settings.get_int("app.scheduler.traffic_poll_minutes", 5), timezone=_TZ)

    def _trig_expiry():
        return IntervalTrigger(minutes=settings.get_int("app.scheduler.expiry_check_minutes", 60), timezone=_TZ)

    def _trig_monitor():
        return IntervalTrigger(minutes=settings.get_int("app.scheduler.monitor_minutes", 3), timezone=_TZ)

    def _trig_monthly():
        return CronTrigger(day=settings.get_int("app.scheduler.monthly_reset_day", 1),
                           hour=settings.get_int("app.scheduler.monthly_reset_hour", 0),
                           minute=0, timezone=_TZ)

    def _trig_backup():
        return CronTrigger(day=settings.get_int("app.scheduler.backup_day", 1),
                           hour=settings.get_int("app.scheduler.backup_hour", 12),
                           minute=0, timezone=_TZ)

    def _trig_purge():
        return CronTrigger(hour=settings.get_int("app.history.purge_hour", 3), minute=0, timezone=_TZ)

    def _trig_update_check():
        """Триггер проверки обновлений по poll_schedule (hour|day|week|month|
        never). never → None (job снимается/не тикает). Время — poll_hour:minute
        (для hour берётся только минута)."""
        sch = str(settings.get("updates.poll_schedule", "day")).lower()
        h = settings.get_int("updates.poll_hour", 10)
        m = settings.get_int("updates.poll_minute", 0)
        if sch == "never":
            return None
        if sch == "hour":
            return CronTrigger(minute=m, timezone=_TZ)
        if sch == "week":
            return CronTrigger(day_of_week=0, hour=h, minute=m, timezone=_TZ)
        if sch == "month":
            return CronTrigger(day=1, hour=h, minute=m, timezone=_TZ)
        return CronTrigger(hour=h, minute=m, timezone=_TZ)     # day (дефолт)

    # ключ настройки → (job_id, фабрика триггера) для горячего reschedule
    _CLASS2 = {
        "app.scheduler.traffic_poll_minutes": ("poll", _trig_poll),
        "app.scheduler.expiry_check_minutes": ("expiry", _trig_expiry),
        "app.scheduler.monitor_minutes": ("monitor", _trig_monitor),
        "app.scheduler.monthly_reset_day": ("monthly", _trig_monthly),
        "app.scheduler.monthly_reset_hour": ("monthly", _trig_monthly),
        "app.scheduler.backup_day": ("backup", _trig_backup),
        "app.scheduler.backup_hour": ("backup", _trig_backup),
        "app.history.purge_hour": ("purge_history", _trig_purge),
    }

    scheduler.add_job(job_poll, _trig_poll(),
                      id="poll", max_instances=1, coalesce=True,
                      next_run_time=now, misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
    scheduler.add_job(job_expiry, _trig_expiry(),
                      id="expiry", max_instances=1, coalesce=True,
                      next_run_time=now, misfire_grace_time=config.MISFIRE_GRACE_EXPIRY_SECONDS)
    scheduler.add_job(job_monthly, _trig_monthly(),
                      id="monthly", max_instances=1, misfire_grace_time=config.MISFIRE_GRACE_CRON_SECONDS)
    scheduler.add_job(job_monthly, "date", run_date=now, id="monthly_catchup",
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
    scheduler.add_job(job_backup, _trig_backup(),
                      id="backup", max_instances=1, misfire_grace_time=config.MISFIRE_GRACE_CRON_SECONDS)
    scheduler.add_job(job_backup, "date", run_date=now, id="backup_catchup",
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
    # ── опрос IMAP: аварийный email-выход из приостановки ────────────────────
    async def job_email_resume():
        from awgbot.infra import email_resume

        def on_code(code: str) -> bool:
            # синхронно (мы уже в to_thread): найти клиента по коду, снять паузу
            ok, notes = services.resume_by_email_code(code)
            if ok:
                # уведомления шлём из loop — соберём и вернём через замыкание
                _pending_notes.extend(notes)
            return ok

        _pending_notes: list = []
        try:
            await asyncio.to_thread(email_resume.poll_once, on_code)
            if _pending_notes:
                await send_notifications(bot, _pending_notes)
        except Exception as e:                        # noqa: BLE001
            log.warning("email_resume poll: %s", e)

    scheduler.add_job(job_monitor, _trig_monitor(),
                      id="monitor", max_instances=1, coalesce=True,
                      next_run_time=now, misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
    # ежедневная очистка истории (батчами), в тихий ночной час
    scheduler.add_job(job_purge_history, _trig_purge(),
                      id="purge_history", max_instances=1,
                      misfire_grace_time=config.MISFIRE_GRACE_CRON_SECONDS)

    # опрос почты для email-выхода — только если фича включена (заданы креды).
    # Интервал из конфига (минимум 60 сек); max_instances=1 — один опрос за раз.
    if config.EMAIL_RESUME_ENABLED:
        scheduler.add_job(job_email_resume,
                          IntervalTrigger(seconds=config.EMAIL_POLL_INTERVAL_SEC, timezone=config.TZ),
                          id="email_resume", max_instances=1, coalesce=True,
                          misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
        log.info("Email-выход из приостановки включён (опрос каждые %d сек)",
                 config.EMAIL_POLL_INTERVAL_SEC)

    # проверка обновлений бота: раз в сутки (по умолчанию 10:00 МСК) + разово на
    # старте (сразу после апдейта увидим следующую ступень, не дожидаясь утра).
    # update_to_notify сам учитывает mute и «ровно один раз на версию».
    async def job_update_check():
        try:
            nxt = await asyncio.to_thread(services.update_to_notify)
            if nxt is not None:
                from awgbot.bot import texts
                from awgbot.bot import keyboards as kb
                await send_notifications(bot, [Notification(
                    config.ADMIN_ID, texts.update_available(nxt.tag, nxt.body),
                    reply_markup=kb.update_notify())])
        except Exception as e:                        # noqa: BLE001
            log.warning("update_check: %s", e)

    _uc_trig = _trig_update_check()
    scheduler.add_job(job_update_check,
                      _uc_trig or CronTrigger(hour=10, minute=0, timezone=_TZ),
                      id="update_check", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_CRON_SECONDS)
    if _uc_trig is None:                              # «никогда» — job есть, но спит
        scheduler.pause_job("update_check")
    scheduler.add_job(job_update_check, "date", run_date=now, id="update_check_startup",
                      max_instances=1, misfire_grace_time=config.MISFIRE_GRACE_CRON_SECONDS)

    # ── Класс 2: горячее перевешивание расписаний по изменению настройки ──────
    # settings.reload()/set_value() зовёт эти хуки ТОЛЬКО по изменившимся ключам
    # (diff), поэтому неизменённые job'ы не трогаем. reschedule_job безопасно
    # звать из потока вотчдога — APScheduler разбудит планировщик сам.
    def _make_reschedule(job_id, factory):
        def hook(key, value):
            try:
                scheduler.reschedule_job(job_id, trigger=factory())
                log.info("расписание '%s' обновлено на лету (%s=%s)", job_id, key, value)
            except Exception as e:                            # noqa: BLE001
                log.warning("reschedule '%s' (%s): %s", job_id, key, e)
        return hook

    for _key, (_job_id, _factory) in _CLASS2.items():
        settings.on_change(_key, _make_reschedule(_job_id, _factory))

    # update_check — особый: poll_schedule=never → триггер None → job на паузу;
    # иначе reschedule (сам снимет паузу, если была). Три ключа ведут сюда.
    def _hook_update_check(key, value):
        try:
            trig = _trig_update_check()
            if trig is None:
                scheduler.pause_job("update_check")
                log.info("проверка обновлений выключена (never)")
            else:
                scheduler.reschedule_job("update_check", trigger=trig)
                log.info("расписание проверки обновлений обновлено (%s=%s)", key, value)
        except Exception as e:                            # noqa: BLE001
            log.warning("reschedule update_check (%s): %s", key, e)

    for _k in ("updates.poll_schedule", "updates.poll_hour", "updates.poll_minute"):
        settings.on_change(_k, _hook_update_check)

    return scheduler


__all__ = ["setup_scheduler"]
