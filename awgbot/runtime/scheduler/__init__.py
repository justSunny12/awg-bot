"""
scheduler/ — фоновые задачи (APScheduler 3.x, AsyncIOScheduler).

Задачи из ТЗ 11:
  • опрос трафика            — каждые TRAFFIC_POLL_MINUTES
  • проверка сроков          — каждые EXPIRY_CHECK_MINUTES + рассылка порогов
  • сброс месячного трафика  — 1-го числа 00:00 по поясу бота (день и час — из
                               настроек; с защитой от двойного)
  • автобэкап                — 1-го числа 12:00 по поясу бота и до получаса позже
                               (почтой или админу файлами)
  • мониторинг               — каждые MONITOR_MINUTES: детект рестарта →
                               реконсиляция блокировок, ребайнд вотчдога,
                               уведомление админа о смене статуса

Сброс и копия — обязательные задания (MANDATORY: misfire_grace_time=None,
coalesce): опоздавший запуск не выбрасывается. Догон на старте (`*_catchup`)
идёт только после назначенного момента месяца (planned_moment_passed);
собранная, но не доставленная копия повторяется через час (`backup_retry`,
у агента — `gw_backup_retry`), пока не уйдёт. Планировщик агента шлюза —
setup_gateway_scheduler, тело месячной копии у ролей общее (monthly_backup).

services синхронны → зовём через asyncio.to_thread, чтобы не морозить loop.

Части: common — общее обеих ролей (проверка обновлений, уведомление о версии,
имена Telegram-аккаунтов, назначенный момент месяца, MANDATORY); backup —
копия по расписанию и по событию (тело месячной копии и триггер — обеих
ролей, задания сервера — add_server_backup); client — планировщик основной роли
(setup_scheduler); gateway — планировщик роли gateway (setup_gateway_scheduler).
"""

from __future__ import annotations

from awgbot.bot.notifier import send_notifications  # noqa: F401 — части зовут через пакет: подменяют тесты

from .common import (log, notify_update_available, TG_NAME_MAX_AGE_DAYS, refresh_tg_names, MANDATORY,
                     planned_moment_passed, _UPDATE_JITTER,
                     update_check_trigger, update_check_hook, add_update_check)
from .backup import monthly_backup, event_backup, BACKUP_JITTER, backup_trigger, add_server_backup
from .client import setup_scheduler
from .gateway import setup_gateway_scheduler

__all__ = ["setup_scheduler", "setup_gateway_scheduler", "send_notifications", "log",
           "notify_update_available", "TG_NAME_MAX_AGE_DAYS", "refresh_tg_names", "MANDATORY",
           "planned_moment_passed", "monthly_backup", "_UPDATE_JITTER", "BACKUP_JITTER",
           "update_check_trigger", "update_check_hook", "add_update_check", "backup_trigger",
           "add_server_backup", "event_backup"]
