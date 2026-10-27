"""jobs.py — часть пакета scheduler (см. __init__): задания данных пользователей,
общие для основного бота и его гостевого режима на резерве: опрос трафика,
сроки и паузы, месячный сброс (с догоном на старте), очистка истории, опрос
почты для выхода из паузы, имена Telegram-аккаунтов. Тела — те же, что были
в планировщике основной роли; здесь они собираются фабрикой по сервисам и
боту, а расписания им назначает вызывающий (у гостя — те же ключи conf
основы: её настройки едут в пакете)."""
from __future__ import annotations

import asyncio

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.runtime import scheduler as pkg          # подменяемое тестами — через пакет

from .common import log


def core_jobs(services, bot, db) -> dict:
    """{имя: корутина} — poll, expiry, monthly, monthly_catchup, purge_history,
    email_resume, tg_names."""

    # ── опрос трафика ────────────────────────────────────────────────────────
    async def job_poll():
        try:
            # poll_traffic возвращает поздравления тем, чьё устройство только
            # что подключилось на новых настройках (обычно их ловит более
            # частый job_migration_watch, см. миграцию).
            greetings = await asyncio.to_thread(services.poll_traffic)
            if await asyncio.to_thread(pkg.following, services):
                return                               # следование: счётчики — да, лимиты и уведомления — у держателя
            await pkg.send_notifications(bot, greetings)
            notifs = await asyncio.to_thread(services.check_traffic_limits)
            await pkg.send_notifications(bot, notifs)
        except Exception as e:                       # noqa: BLE001
            log.warning("poll_traffic: %s", e)

    # ── проверка сроков + уведомления ────────────────────────────────────────
    async def job_expiry():
        if await asyncio.to_thread(pkg.following, services):
            return
        try:
            notifs = await asyncio.to_thread(services.check_expiry)
            # прикрепить кнопку отсрочки к отмеченным уведомлениям (UI-слой —
            # scheduler-композиция, чтобы services не тянул keyboards)
            from awgbot.bot import keyboards as kb
            for n in notifs:
                if getattr(n, "grace_offer_client_id", 0):
                    n.reply_markup = kb.grace_offer(n.grace_offer_client_id, settings.get_int("grace.grace_days", 14))
            await pkg.send_notifications(bot, notifs)
            # авто-выход из приостановок по истечении зарезервированного срока
            pause_notes = await asyncio.to_thread(services.check_pauses)
            await pkg.send_notifications(bot, pause_notes)
        except Exception as e:                       # noqa: BLE001
            log.warning("check_expiry: %s", e)

    # ── сброс месячного трафика ──────────────────────────────────────────────
    # Guard по «году-месяцу» даёт и защиту от двойного запуска, и catch-up:
    # задача дополнительно прогоняется на старте (date-триггер), поэтому
    # пропущенный из-за даунтайма cron 1-го числа навёрстывается при первом же
    # запуске в новом месяце. Первый запуск бота (state пуст) сброс не делает —
    # нечего сбрасывать, просто фиксирует текущий месяц.
    async def job_monthly(catchup: bool = False):
        ym = timeutil.now().strftime("%Y-%m")
        last = await asyncio.to_thread(db.get_state, "last_monthly_reset")
        if last and last >= ym:
            # сделано — и когда записанный месяц новее «сейчас»: смена пояса на
            # западный в первые часы 1-го числа вернула бы прошлый месяц, и сброс
            # (с рассылкой пользователям) прошёл бы второй раз
            return
        if catchup and not pkg.planned_moment_passed(settings.get_int("app.scheduler.monthly_reset_day", 1),
                                                 settings.get_int("app.scheduler.monthly_reset_hour", 0)):
            return                                   # назначенный момент ещё впереди — крон своё сделает
        if last is None:
            await asyncio.to_thread(db.set_state, "last_monthly_reset", ym)   # первый запуск — только фиксация
            return
        if await asyncio.to_thread(pkg.following, services):
            # сброс делает держатель (бот основы на резерве); после возврата со слиянием он
            # учтён, без слияния — догон после возврата сделает его здесь
            await asyncio.to_thread(services.reserve_monthly_defer, ym)
            return
        try:
            reset_notes = await asyncio.to_thread(services.reset_monthly_traffic)
            await asyncio.to_thread(db.set_state, "last_monthly_reset", ym)
            await pkg.send_notifications(bot, reset_notes)
            log.info("Месячный трафик сброшен (catch-up или cron) за %s", ym)
        except Exception as e:                       # noqa: BLE001
            log.warning("monthly_reset: %s", e)

    async def job_monthly_catchup():
        await job_monthly(catchup=True)

    async def job_purge_history():
        """Ежедневно: удалить историю старше ретеншна (батчами). Идемпотентно —
        если чистить нечего, просто отработает вхолостую."""
        try:
            removed = await asyncio.to_thread(services.purge_old_history)
            if removed:
                log.info("Очистка истории: удалено %s", removed)
        except Exception as e:                       # noqa: BLE001
            log.warning("purge_history: %s", e)

    # ── опрос IMAP: аварийный email-выход из приостановки ────────────────────
    async def job_email_resume():
        from awgbot.infra import email_resume

        def on_code(code: str) -> bool:
            # синхронно (мы уже в to_thread): найти пользователя по коду, снять паузу
            ok, notes = services.resume_by_email_code(code)
            if ok:
                # уведомления шлём из loop — соберём и вернём через замыкание
                _pending_notes.extend(notes)
            return ok

        _pending_notes: list = []
        if await asyncio.to_thread(pkg.following, services):
            return                                   # ящик читает держатель: двоим один код не разделить
        try:
            if not await asyncio.to_thread(services.email_resume_enabled):
                return
            # Пока ни у кого нет активной паузы с кодом — читать письма незачем:
            # TLS + LOGIN + семь папок каждую минуту ради пустоты.
            if not await asyncio.to_thread(services.db.any_active_resume_code):
                return
            acc = await asyncio.to_thread(services.email_account)
            await asyncio.to_thread(email_resume.poll_once, acc, on_code)
            if _pending_notes:
                await pkg.send_notifications(bot, _pending_notes)
        except Exception as e:                        # noqa: BLE001
            log.warning("email_resume poll: %s", e)

    # имена Telegram-аккаунтов: разово на старте (после обновления они пусты у
    # всех) и раз в сутки для тех, кто давно не писал; в сеть — с джиттером
    async def job_tg_names():
        if await asyncio.to_thread(pkg.following, services):
            return
        try:
            n = await pkg.refresh_tg_names(bot, db)
            if n:
                log.info("имена Telegram-аккаунтов обновлены: %d", n)
        except Exception as e:                        # noqa: BLE001
            log.warning("tg_names: %s", e)

    return {"poll": job_poll, "expiry": job_expiry, "monthly": job_monthly, "monthly_catchup": job_monthly_catchup,
            "purge_history": job_purge_history, "email_resume": job_email_resume, "tg_names": job_tg_names}
