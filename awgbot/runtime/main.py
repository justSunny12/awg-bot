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
import hashlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from awgbot.core import config
from awgbot.core import settings
from awgbot.infra import awg
from awgbot.infra.db import Database
from awgbot.domain.services import Services
from awgbot.bot.middleware import AccessMiddleware
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.runtime.scheduler import setup_scheduler
from awgbot.runtime.watcher import AwgWatcher
from awgbot.runtime.conf_watcher import ConfWatcher
from awgbot.bot.handlers import admin as admin_handlers
from awgbot.bot.handlers import settings as settings_handlers
from awgbot.bot.handlers import reply_commands as reply_commands_handlers
from awgbot.bot.handlers import client as client_handlers
from awgbot.bot.handlers import friend as friend_handlers
from awgbot.bot.handlers import guide as guide_handlers
from awgbot.bot.handlers import routing as routing_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("awgbot.main")


async def _notify_migration_needed(bot: Bot, services: Services) -> None:
    """Сообщить админу, что нужен переезд профилей. Условия: манифест поставки
    новее применённого поколения, рычаг переезда настроен (второй интерфейс
    поднят установщиком) и переезд ещё не идёт."""
    from awgbot.bot import keyboards as kb
    from awgbot.bot import texts
    from awgbot.infra import awglock
    if not awglock.needs_migration():
        return
    if await asyncio.to_thread(services.migration_running):
        return
    if not await asyncio.to_thread(services.migration_available):
        # Рычага нет: второй интерфейс не поднят (установщик его не поднимал,
        # например пока шёл другой переезд). Предлагаем поднять кнопкой — иначе
        # тупик: интерфейс заводит только обновление, а оно уже прошло.
        await notify_one(bot, config.ADMIN_ID, texts.migration_generation_pending(),
                         reply_markup=kb.migration_generation_pending())
        return
    await notify_one(bot, config.ADMIN_ID,
                     texts.migration_needed(awglock.generation(), awglock.applied_generation()),
                     reply_markup=kb.migration_needed())


async def _notify_private_dns_offer(bot: Bot, services: Services) -> None:
    """Предложить свой DNS-резолвер: адрес в конфигах публичный, админ ещё не
    решал. Три кнопки — три решения, любое гасит инфобокс."""
    from awgbot.bot import keyboards as kb
    from awgbot.bot import texts
    if not await asyncio.to_thread(services.private_dns_offer_due):
        return
    info = await asyncio.to_thread(services.private_dns_info)
    await notify_one(bot, config.ADMIN_ID, texts.private_dns_offer(info["target"]),
                     reply_markup=kb.private_dns_offer_kb())


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


async def report_update_result(bot, services) -> None:
    """Финишер self-update: убрать «дождись», отчитаться, ЕДИНСТВЕННАЯ живая
    кнопка «В меню».

    Обновление через несколько ступеней даёт цепочку рестартов, и финишер
    приходит после каждого. Кнопку прежнего снимаем при отправке следующего:
    остаться она должна только на последнем — иначе в чате столько же живых
    «В меню», сколько было ступеней, и все ведут в одно место. Тексты при этом
    не трогаем: сама история «какая ступень чем закончилась» ценна.
    """
    wait = await asyncio.to_thread(services.pop_update_wait)
    note = await asyncio.to_thread(services.confirm_applied_update)
    if wait is not None:                                 # прибрать «дождись» всегда
        try:
            await bot.delete_message(chat_id=wait[0], message_id=wait[1])
        except Exception:                               # noqa: BLE001
            pass
    if note is None:
        return
    # кнопки у ВСЕХ прошлых окон обновления — долой (история, не один id:
    # между финишерами бывают «не применилось» и «не удалось» с той же кнопкой)
    from awgbot.bot.handlers.common import dismiss_update_reports
    await dismiss_update_reports(bot, services)
    sent = await notify_one(bot, note.tg_id, note.text,
                            reply_markup=note.reply_markup)
    if sent is not None:
        await asyncio.to_thread(services.remember_update_report,
                                sent.chat.id, sent.message_id)


async def _sync_bot_identity(bot: Bot, db: Database) -> bool:
    """Публичное имя/описания бота — из conf/bot_identity.yaml (маскирующие
    формулировки, ничего не должно выдавать назначение бота стороннему
    наблюдателю профиля). Правки, вбитые вручную в BotFather, переживут только
    до следующего рестарта — дальше их перетрёт этот блок.

    Меню команд (кнопка «/») НЕ регистрируем и явно СТИРАЕМ: раньше сюда уже
    отправлялся /code, Bot API хранит это до явной перезаписи.

    Отпечаток (имя, описания, версия) — в state: совпал — ни одного запроса к
    Bot API (раньше каждый старт стоил три-четыре, а set_my_name Telegram
    ещё и рейт-лимитит). Возвращает True, если запросы делались."""
    ident = hashlib.sha256("|".join((config.BOT_NAME or "", config.BOT_DESCRIPTION or "",
                                     config.BOT_SHORT_DESCRIPTION or "",
                                     config.INSTALLED_VERSION)).encode()).hexdigest()[:16]
    if db.get_state("bot_identity") == ident:
        return False
    if config.BOT_NAME and (await bot.get_my_name()).name != config.BOT_NAME:
        await bot.set_my_name(config.BOT_NAME)
    if config.BOT_DESCRIPTION and \
            (await bot.get_my_description()).description != config.BOT_DESCRIPTION:
        await bot.set_my_description(config.BOT_DESCRIPTION)
    if config.BOT_SHORT_DESCRIPTION and \
            (await bot.get_my_short_description()).short_description != config.BOT_SHORT_DESCRIPTION:
        await bot.set_my_short_description(config.BOT_SHORT_DESCRIPTION)
    await bot.delete_my_commands()
    db.set_state("bot_identity", ident)
    return True


async def _announce_reboot(bot, db, who: str) -> None:
    """«Хост перезагружен, запуск успешен» — после всех проверок старта. Только
    когда хост действительно перезагружался (boot_id сменился), не на каждый
    рестарт сервиса."""
    from awgbot.runtime import hostboot
    try:
        if not await asyncio.to_thread(hostboot.reboot_detected, db):
            return
        import socket
        from awgbot.bot.notifier import notify_one
        from awgbot.bot import texts
        await notify_one(bot, config.ADMIN_ID, texts.host_rebooted(socket.gethostname(), who))
    except Exception as e:                               # noqa: BLE001
        log.warning("announce_reboot: %s", e)


async def run_gateway() -> None:
    """Сборка и запуск роли gateway: панель + монитор, больше ничего.

    Дублирование пары строк с клиентской сборкой (Bot, Dispatcher, middleware)
    осознанное: общий «конструктор с ветками» связал бы роли ровно там, где им
    положено не знать друг о друге.
    """
    from awgbot.domain.gateway import GatewayServices
    from awgbot.bot.handlers import gateway as gateway_handlers
    from awgbot.runtime.scheduler import setup_gateway_scheduler
    from awgbot.runtime import preflight

    db = Database(config.DB_PATH)
    db.init_schema()
    services = GatewayServices(db)

    # Шлюз стоит в юрисдикции, где Telegram заблокирован, и ходит к нему через
    # туннель до ВПС — по метке, как всё помеченное на этой машине. Туннель
    # только IPv4, а резолвер отдаёт api.telegram.org сначала AAAA-адресом:
    # aiohttp полез бы по v6 в никуда и утонул в таймауте ещё до v4-попытки.
    # Прибиваем сессию к IPv4. _connector_init — приватное поле aiogram, но
    # это единственная точка, куда доезжают параметры TCPConnector; отказ его
    # заполнить не фатален — тогда просто работаем как обычно.
    session = None
    if settings.get_bool("app.gateway.ipv4_only", True):
        try:
            import socket
            from aiogram.client.session.aiohttp import AiohttpSession
            session = AiohttpSession()
            session._connector_init["family"] = socket.AF_INET   # noqa: SLF001
        except Exception as e:                           # noqa: BLE001
            log.warning("gateway: не смог ограничить сессию IPv4: %s", e)
            session = None
    bot = Bot(config.BOT_TOKEN, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                           link_preview_is_disabled=True))
    try:
        await bot.get_me()
    except TelegramUnauthorizedError as e:
        raise preflight.PreflightError(
            f"Bot API отверг токен (getMe: {e}). Проверьте BOT_TOKEN в "
            f"/etc/awg-bot/env и перезапустите.") from e
    except Exception as e:                               # noqa: BLE001
        log.warning("getMe на старте не прошёл (сеть ещё не готова?): %s — "
                    "продолжаю, polling дождётся сети", e)

    dp = Dispatcher(storage=MemoryStorage())
    dp["services"] = services
    access = AccessMiddleware(db)          # клиентов в БД нет: пускает админа,
    dp.message.outer_middleware(access)    # остальных молча роняет — ровно то,
    dp.callback_query.outer_middleware(access)  # что шлюзу и нужно
    dp.include_router(gateway_handlers.router)

    conf_watcher = ConfWatcher(config.CONF_DIR)
    conf_watcher.start()

    from awgbot.bot import notifier as _notifier

    async def _mail_fallback(text: str) -> None:
        if await asyncio.to_thread(services.email_alert_fallback_enabled):
            await asyncio.to_thread(services.email_send_alert, text)
    _notifier.set_email_fallback(_mail_fallback)

    try:
        warns = preflight.collect_warnings_gateway(services)
        if warns:
            from awgbot.bot.notifier import notify_one
            await notify_one(bot, config.ADMIN_ID, preflight.format_warnings(warns))
    except Exception as e:                               # noqa: BLE001
        log.warning("gateway preflight warnings: %s", e)

    # Итог self-update — тем же путём, что у клиентской роли: убрать «дождись»,
    # отчитаться, единственная живая кнопка «В меню».
    try:
        await report_update_result(bot, services)
    except Exception as e:                               # noqa: BLE001
        log.warning("gateway confirm_applied_update: %s", e)

    # Обещание «вернётся через несколько секунд» после «Перезапустить бота» —
    # исполняет новый процесс, как у основной роли.
    try:
        await gateway_handlers.restore_panel_after_restart(bot, services)
    except Exception as e:                               # noqa: BLE001
        log.warning("gateway restore_panel_after_restart: %s", e)

    # Обещание установщика «бот напишет сам» после опознания кодом.
    try:
        await gateway_handlers.send_first_panel(bot, services)
    except Exception as e:                               # noqa: BLE001
        log.warning("gateway send_first_panel: %s", e)

    await _announce_reboot(bot, services.db, "агента")
    try:
        from awgbot.bot.handlers.restore import report_restore_result
        await report_restore_result(bot, services)
    except Exception as e:                               # noqa: BLE001
        log.warning("report_restore_result: %s", e)

    # Планировщик — СТРОГО после финишера, как у клиентской роли. Его стартовая
    # проверка обновлений укладывается в секунду, и, запущенная раньше, она
    # ставила «доступна следующая» ВЫШЕ «обновлён до…»: последним в чате висел
    # отчёт, а уведомление о следующей ступени терялось над ним.
    scheduler = setup_gateway_scheduler(services, bot)

    log.info("Агент шлюза запущен (роль gateway)")
    try:
        await dp.start_polling(bot, polling_timeout=50,
                               allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        conf_watcher.stop()
        log.info("Останавливаюсь…")


async def main() -> None:
    config.validate()
    from awgbot.runtime import preflight
    preflight.check_fatal()                 # стоп-факторы: data-dir, целостность БД
    settings.init(config.CONF_DIR)          # горячий кэш conf/*.yaml (до чтений)

    if config.ROLE == "gateway":
        # Роль агента шлюза — свой мир целиком (docs/ROADMAP.md, п.7): ни
        # клиентов, ни awg-сервера, ни вотчдога конфига. Ветвимся РАНО, а не
        # флажками по всему клиентскому пути: пропущенный флажок здесь
        # означал бы «агент полез в docker за awg» — молча и не туда.
        await run_gateway()
        return

    db = Database(config.DB_PATH)
    db.init_schema()
    services = Services(db)
    services.ensure_admin_client()          # админ — тоже пользователь VPN
    services.migrate_pause_balances()       # v2.22.0: счёт дней паузы — разово

    bot = Bot(
        config.BOT_TOKEN,
        # Превью ссылок выключены для всех сообщений: уведомление об обновлении
        # несёт список ссылок на релизы, и карточка первой из них — шум.
        default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                     link_preview_is_disabled=True),
    )
    # Токен: getMe ловит протухший/отозванный токен сразу (внятное сообщение
    # вместо «молчаливого» бота). ВАЖНО: fatal — ТОЛЬКО явный отказ Bot API
    # (401 Unauthorized). Сетевые ошибки (VPS стартовал раньше сети/DNS) не
    # фатальны: polling ниже ретраится сам, а fatal-цикл здесь исчерпал бы
    # systemd StartLimit и уложил бота там, где надо было подождать 30 секунд.
    try:
        me = await bot.get_me()
        services.bot_username = me.username or ""
    except TelegramUnauthorizedError as e:
        raise preflight.PreflightError(
            f"Bot API отверг токен (getMe: {e}). Проверьте BOT_TOKEN в "
            f"/etc/awg-bot/env и перезапустите.") from e
    except Exception as e:                               # noqa: BLE001
        log.warning("getMe на старте не прошёл (сеть ещё не готова?): %s — "
                    "продолжаю, polling дождётся сети", e)
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
    # ДО client: у обоих роль client, и FSM-состояние ввода адресов должно
    # ловиться здесь, а не общим message-хендлером клиента
    dp.include_router(routing_handlers.router)
    dp.include_router(client_handlers.router)

    loop = asyncio.get_running_loop()

    def on_change() -> None:
        """Вызывается из потока вотчдога — планируем async-реконсиляцию на loop."""
        asyncio.run_coroutine_threadsafe(do_reconcile(services, bot), loop)

    watcher = AwgWatcher(on_change)
    conf_watcher = ConfWatcher(config.CONF_DIR)   # горячая правка настроек
    conf_watcher.start()
    scheduler = setup_scheduler(services, bot, db, watcher)

    # запасной канал для критичных алертов: почта, когда Telegram не отвечает
    from awgbot.bot import notifier as _notifier

    async def _mail_fallback(text: str) -> None:
        if await asyncio.to_thread(services.email_alert_fallback_enabled):
            await asyncio.to_thread(services.email_send_alert, text)
    _notifier.set_email_fallback(_mail_fallback)

    # ── стартовые задачи ─────────────────────────────────────────────────────
    # seed детекта рестарта (сохранит текущий StartedAt, реконсиляции не будет —
    # первый запуск); seed статуса сервера, чтобы monitor не слал ложный алерт.
    await asyncio.to_thread(services.detect_and_handle_restart)
    # Эндшпиль переезда профилей: после переключения дефолтного интерфейса
    # явные метки iface, равные ему, схлопываются в пустую строку — две записи
    # одного смысла это класс расхождения, который здесь уже стрелял. Вне
    # эндшпиля UPDATE холостой.
    try:
        n = await asyncio.to_thread(services.db.normalize_default_iface,
                                    config.AWG_INTERFACE)
        if n:
            log.info("миграция: нормализовано устройств: %d (iface → дефолт)", n)
    except Exception as e:                               # noqa: BLE001
        log.warning("normalize_default_iface: %s", e)
    startup_ok: bool | None = None
    try:
        startup_ok = await asyncio.to_thread(services.server_ok)
        db.set_state("last_server_ok", "1" if startup_ok else "0")
    except Exception:                                    # noqa: BLE001
        pass

    # Первое устройство админа — до реконсиляции: без него он не достучится до
    # бота, а завести его снаружи больше нельзя (неизвестные пиры уходят в
    # карантин). Сбой не фатален — повторится на следующем старте.
    try:
        _boot_dev = await asyncio.to_thread(services.bootstrap_admin_device)
        if _boot_dev is not None:
            from awgbot.bot import texts as _texts
            from awgbot.bot.notifier import notify_one as _notify
            # Через notifier, а не bot.send_message: он подставляет «Скрыть», а
            # ссылка — секрет, и висеть в чате вечно ей нельзя. force_sound:
            # админ ждёт доступа, тихие часы тут не та цена.
            await _notify(bot, config.ADMIN_ID,
                          _texts.admin_bootstrap_device(_boot_dev.address),
                          force_sound=True)
            await _notify(bot, config.ADMIN_ID, f"<code>{_boot_dev.vpn}</code>",
                          force_sound=True)
    except Exception as e:                               # noqa: BLE001
        log.warning("bootstrap_admin_device: %s", e)

    await do_reconcile(services, bot)                    # сверка состава пиров
    try:
        await asyncio.to_thread(services.reconcile_blocks)   # восстановить блокировки
    except Exception as e:                               # noqa: BLE001
        log.warning("reconcile_blocks на старте: %s", e)
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

    # Поставка привезла ядро нового поколения — переезд профилей обязателен, и
    # сказать об этом надо при КАЖДОМ старте, пока он не начат: пропустить такое
    # сообщение один раз значит остаться на ядре, которое скоро перестанет
    # обслуживать клиентов, и не понять почему.
    try:
        await _notify_migration_needed(bot, services)
    except Exception as e:                               # noqa: BLE001
        log.warning("migration_needed: %s", e)
    # DNS клиентов публичный, а решения ещё нет — предлагать при каждом старте:
    # любой из трёх ответов инфобокс гасит.
    try:
        await _notify_private_dns_offer(bot, services)
    except Exception as e:                               # noqa: BLE001
        log.warning("private_dns_offer: %s", e)

    # Тот же принцип для обычного рестарта из настроек: обещание «вернётся через
    # несколько секунд» исполняет новый процесс. Отдельно от блока выше — там
    # итог обновления, который обязан остаться в истории, здесь же сообщение
    # служебное и подменяется панелью.
    try:
        from awgbot.bot.handlers.admin import restore_panel_after_restart
        await restore_panel_after_restart(bot, services)
    except Exception as e:                               # noqa: BLE001
        log.warning("restore_panel_after_restart: %s", e)

    watcher.ensure_watching()
    scheduler.start()
    log.info("Бот запущен")

    # ── после старта поллинга: всё, без чего первый апдейт обслуживается ──
    # Раньше это шло до getUpdates и стоило 10–30 с молчания на каждом старте:
    # списки маршрутизации (HTTP, до 15 с на источник), зонд шлюза и вход по
    # IMAP в замечаниях, четыре запроса к Bot API за именем. Бот отвечает через
    # пару секунд, а это догоняет в фоне; сбой любого шага — в лог, не наружу.
    async def _after_start() -> None:
        try:
            await asyncio.to_thread(services.reconcile_ssh_access)  # SSH из туннеля: set устройств админа
            await asyncio.to_thread(services.retire_legacy_ssh_gate)
        except Exception as e:                           # noqa: BLE001
            log.warning("reconcile_ssh_access на старте: %s", e)
        try:
            # Слоты шлюзов (docs/gateway-failover.md): юнит первого линка —
            # на шаблон, активный на холодном старте — предпочтительный.
            await asyncio.to_thread(services.gateway_units_migrate)
            # порт линка правят руками в конфиге ВПС — строка слота узнаёт сама
            for note in await asyncio.to_thread(services.gateway_sync_link_ports):
                await notify_one(bot, note.tg_id, note.text)
            await asyncio.to_thread(services.routing_cold_start)
        except Exception as e:                           # noqa: BLE001
            log.warning("слоты шлюзов на старте: %s", e)
        try:
            # Списки условной маршрутизации — на старте, а не руками до него.
            # Метод сам решает, пора ли обновлять; при пустом кэше делает это
            # немедленно: без списков режим не действует вовсе. Пока качаются,
            # действуют прежние правила — это не отказ.
            await asyncio.to_thread(services.routing_update_lists)
            await asyncio.to_thread(services.reconcile_routing)
        except Exception as e:                           # noqa: BLE001
            log.warning("routing на старте: %s", e)
        try:
            await _sync_bot_identity(bot, db)
        except Exception as e:                           # noqa: BLE001
            log.warning("set_my_name/description/delete_commands: %s", e)
        # Замечания preflight — первым содержательным сообщением админу. Через
        # notifier: он и тихие часы соблюдает, и подставляет «Скрыть» — это
        # проактивное уведомление, админ его не заказывал.
        try:
            warns = await asyncio.to_thread(preflight.collect_warnings, services, startup_ok)
            if warns:
                await notify_one(bot, config.ADMIN_ID, preflight.format_warnings(warns))
        except Exception as e:                           # noqa: BLE001
            log.warning("preflight warnings: %s", e)
        await _announce_reboot(bot, db, "бота")
        try:
            from awgbot.bot.handlers.restore import report_restore_result
            await report_restore_result(bot, services)
        except Exception as e:                           # noqa: BLE001
            log.warning("report_restore_result: %s", e)
        log.info("стартовые фоновые задачи завершены")

    after_start = asyncio.create_task(_after_start())
    after_start.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

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
