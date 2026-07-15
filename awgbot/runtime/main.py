"""
main.py — точка входа. Собирает всё вместе и запускает бота.

Порядок старта:
  1. валидация секретов, инициализация БД
  2. бот (HTML parse_mode) + диспетчер + middleware + роутеры
  3. seed детекта рестарта и статуса сервера
  4. первичная реконсиляция состава пиров и блокировок
  5. вотчдог (inotify) + планировщик (APScheduler)
  6. polling до остановки; на выходе — аккуратное закрытие
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import awg
from awgbot.infra.db import Database
from awgbot.domain.services import Services
from awgbot.bot.middleware import AccessMiddleware
from awgbot.bot.notifier import send_notifications
from awgbot.runtime.scheduler import setup_scheduler
from awgbot.runtime.watcher import AwgWatcher
from awgbot.runtime.conf_watcher import ConfWatcher
from awgbot.bot.handlers import admin as admin_handlers
from awgbot.bot.handlers import settings as settings_handlers
from awgbot.bot.handlers import reply_commands as reply_commands_handlers
from awgbot.bot.handlers import client as client_handlers
from awgbot.bot.handlers import friend as friend_handlers
from awgbot.bot.handlers import guide as guide_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("awgbot.main")


async def do_reconcile(services: Services, bot: Bot) -> None:
    """Реконсиляция состава пиров + рассылка уведомлений (вызов из вотчдога и старта).
    Внешнее изменение файлов могло затронуть и [Interface] — сбрасываем кэш
    серверных параметров (следующее чтение возьмёт живые значения)."""
    try:
        awg.invalidate_server_params()
        notifs = await asyncio.to_thread(services.reconcile_peers)
        await send_notifications(bot, notifs)
    except Exception as e:                               # noqa: BLE001
        log.warning("reconcile_peers: %s", e)


async def main() -> None:
    config.validate()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings.init(config.CONF_DIR)          # горячий кэш conf/*.yaml (до чтений)

    db = Database(config.DB_PATH)
    db.init_schema()
    services = Services(db)
    services.ensure_admin_client()          # админ — тоже пользователь VPN

    bot = Bot(
        config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp["services"] = services

    access = AccessMiddleware(db)
    # ВАЖНО: outer_middleware — отрабатывает ДО фильтров роутеров. RoleFilter на
    # роутерах читает data['role'], который кладёт этот middleware; при обычном
    # .middleware() (inner) фильтры выполнились бы раньше и role ещё не было бы.
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)

    dp.include_router(reply_commands_handlers.router)   # ПЕРВЫМ: reply-команды бьют раньше FSM
    dp.include_router(admin_handlers.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(guide_handlers.router)
    dp.include_router(friend_handlers.router)
    dp.include_router(client_handlers.router)

    loop = asyncio.get_running_loop()

    def on_change() -> None:
        """Вызывается из потока вотчдога — планируем async-реконсиляцию на loop."""
        asyncio.run_coroutine_threadsafe(do_reconcile(services, bot), loop)

    watcher = AwgWatcher(on_change)
    conf_watcher = ConfWatcher(config.CONF_DIR)   # горячая правка настроек
    conf_watcher.start()
    scheduler = setup_scheduler(services, bot, db, watcher)

    # ── стартовые задачи ─────────────────────────────────────────────────────
    # seed детекта рестарта (сохранит текущий StartedAt, реконсиляции не будет —
    # первый запуск); seed статуса сервера, чтобы monitor не слал ложный алерт.
    await asyncio.to_thread(services.detect_and_handle_restart)
    try:
        ok = await asyncio.to_thread(services.server_ok)
        db.set_state("last_server_ok", "1" if ok else "0")
    except Exception:                                    # noqa: BLE001
        pass

    await do_reconcile(services, bot)                    # подхватить app-устройства
    try:
        await asyncio.to_thread(services.reconcile_blocks)   # восстановить блокировки
    except Exception as e:                               # noqa: BLE001
        log.warning("reconcile_blocks на старте: %s", e)
    try:
        await asyncio.to_thread(services.reconcile_ssh_access)  # пер-пирный SSH-к-хосту
    except Exception as e:                               # noqa: BLE001
        log.warning("reconcile_ssh_access на старте: %s", e)

    # итог self-update: если перед рестартом запускалось обновление — удалить
    # «дождись завершения» и отчитаться админу («успешно обновлен…» + changelog
    # с кнопкой «В меню» / «не применилось»). Флаги стираются однократно.
    try:
        wait = await asyncio.to_thread(services.pop_update_wait)
        note = await asyncio.to_thread(services.confirm_applied_update)
        if wait is not None:                             # прибрать «дождись» всегда
            try:
                await bot.delete_message(chat_id=wait[0], message_id=wait[1])
            except Exception:                           # noqa: BLE001
                pass
        if note is not None:
            await send_notifications(bot, [note])
    except Exception as e:                               # noqa: BLE001
        log.warning("confirm_applied_update: %s", e)

    # Публичное имя/описания бота — из conf/bot_identity.yaml (маскирующие
    # формулировки, ничего не должно выдавать назначение бота стороннему
    # наблюдателю профиля). Правки, вбитые вручную в BotFather, переживут
    # только до следующего рестарта — дальше их перетрёт этот блок.
    #
    # Меню команд (кнопка «/») НЕ регистрируем и явно СТИРАЕМ: пусто по
    # умолчанию у нового бота, но раньше сюда уже отправлялся /code — Bot API
    # хранит это на своей стороне до явной перезаписи, простое прекращение
    # set_my_commands() старую запись не уберёт. /code уже объясняется текстом
    # на /start (COLD_START_GREETING), лишняя публичная подсказка не нужна.
    try:
        # set_my_name жёстко рейт-лимитится Telegram'ом (смена имени — редкая
        # операция), а мы рестартуем чаще, чем меняем identity. Сравниваем с
        # текущим и пишем только при реальном отличии — без flood-warning'ов в
        # логах и лишних записей на стороне Bot API.
        if config.BOT_NAME and (await bot.get_my_name()).name != config.BOT_NAME:
            await bot.set_my_name(config.BOT_NAME)
        if config.BOT_DESCRIPTION and \
                (await bot.get_my_description()).description != config.BOT_DESCRIPTION:
            await bot.set_my_description(config.BOT_DESCRIPTION)
        if config.BOT_SHORT_DESCRIPTION and \
                (await bot.get_my_short_description()).short_description != config.BOT_SHORT_DESCRIPTION:
            await bot.set_my_short_description(config.BOT_SHORT_DESCRIPTION)
        await bot.delete_my_commands()
    except Exception as e:                               # noqa: BLE001
        log.warning("set_my_name/description/delete_commands: %s", e)

    watcher.ensure_watching()
    scheduler.start()
    log.info("Бот запущен")

    # Подсветка админу: не назначено устройство полного доступа (см. концепт).
    # Шлём при каждом старте, пока не назначено ИЛИ не нажато «Игнорировать».
    try:
        if await asyncio.to_thread(services.admin_fa_hint_needed):
            from awgbot.bot import texts, keyboards as kb
            await bot.send_message(config.ADMIN_ID, texts.ADMIN_FA_HINT,
                                   reply_markup=kb.admin_fa_hint())
    except Exception as e:                       # noqa: BLE001
        log.warning("fa-hint: %s", e)

    try:
        # long-poll 50 с вместо дефолтных 10: впятеро меньше холостых
        # getUpdates-запросов (TLS/CPU/сеть) на простаивающем боте; на задержку
        # доставки не влияет — Telegram отвечает мгновенно при событии.
        # allowed_updates задаём ЯВНО из зарегистрированных типов: иначе при
        # некоторых конфигурациях getUpdates может не запросить нужные апдейты
        # (например, message с deep-link /start), и бот «молчит» на инвайт-ссылку.
        await dp.start_polling(bot, polling_timeout=50,
                               allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Останавливаюсь…")
        scheduler.shutdown(wait=False)
        watcher.stop()
        conf_watcher.stop()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
