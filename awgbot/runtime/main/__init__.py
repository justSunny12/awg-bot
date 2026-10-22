"""
main/ — точка входа. Собирает всё вместе и запускает бота.

Порядок старта:
  1. валидация секретов, инициализация БД
  2. бот (HTML parse_mode) + диспетчер роли (bot/routers.py: middleware и
     роутеры в порядке включения — один источник для запуска и тестов)
  3. seed детекта рестарта и статуса сервера
  4. первичная реконсиляция состава пиров и блокировок
  5. вотчдог (inotify) + планировщик (APScheduler)
  6. polling до остановки; на выходе — аккуратное закрытие

Части: common — журнал (под journald без времени в строке, вне — ISO со
смещением в поясе бота; логгер apscheduler — только WARNING, задания резерва
идут раз в несколько секунд), строка пояса на старте и метки в каталоге
данных;
startup — стартовые уведомления и финишеры (меню команд, переезд, свой DNS,
реконсиляция, итог обновления, имя бота, перезагрузка хоста, пояс после
обновления); alive — отметка жизни для сторожа обновления по ответу
Telegram на getUpdates; gateway — сборка и запуск роли
gateway (run_gateway); guest — бот основы на резерве в гостевом режиме, без
поллинга, со слушателем вебхука (run_guest); join — агент шлюза в режиме
сопряжения, без Telegram (run_join); client — сборка и запуск основной роли (main).
Запуск модулем — __main__.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramUnauthorizedError  # noqa: F401
from aiogram.client.default import DefaultBotProperties  # noqa: F401
from aiogram.enums import ParseMode  # noqa: F401

from awgbot.core import config  # noqa: F401
from awgbot.core import settings  # noqa: F401
from awgbot.infra import awg  # noqa: F401
from awgbot.infra.db import Database
from awgbot.domain.services import Services
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.bot.routers import make_dispatcher
from awgbot.runtime.scheduler import setup_scheduler
from awgbot.runtime.watcher import AwgWatcher
from awgbot.runtime.conf_watcher import ConfWatcher

from .common import FRESH_INSTALL_MARKER, ZONE_ADOPTED_MARKER, log, log_formatter, log_zone  # noqa: E402

# Журнал: под journald время ставит journald, вне — ISO со смещением в поясе бота.
_log_handler = logging.StreamHandler()
_log_handler.setFormatter(log_formatter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
# задания планировщика резерва идут раз в 3–5 с: их «Running/executed job» на
# INFO вытесняли бы из журнала всё остальное за минуты — только сбои
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# части зовут через пакет всё, что подменяют тесты (Bot, Database, Services,
# ConfWatcher, make_dispatcher, notify_one, функции старта)
from .startup import (_menu_command, _notify_migration_needed, _notify_private_dns_offer,  # noqa: E402
                      do_reconcile, report_update_result, _sync_bot_identity, _announce_reboot,
                      _announce_zone)
from .gateway import run_gateway  # noqa: E402
from .guest import run_guest  # noqa: E402
from .join import join_pending, run_join  # noqa: E402
from .client import main  # noqa: E402

__all__ = ["Bot", "TelegramUnauthorizedError", "DefaultBotProperties", "ParseMode", "config", "settings", "awg",
           "Database", "Services", "notify_one", "send_notifications", "make_dispatcher", "setup_scheduler",
           "AwgWatcher", "ConfWatcher", "FRESH_INSTALL_MARKER", "ZONE_ADOPTED_MARKER", "log", "log_zone",
           "_menu_command", "_notify_migration_needed", "_notify_private_dns_offer", "do_reconcile",
           "report_update_result", "_sync_bot_identity", "_announce_reboot", "_announce_zone",
           "run_gateway", "run_guest", "join_pending", "run_join", "main"]
