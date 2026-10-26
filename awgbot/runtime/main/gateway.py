"""gateway.py — часть пакета main (см. __init__): сборка и запуск роли gateway (run_gateway)."""

from __future__ import annotations

import asyncio
import contextlib
import os

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from awgbot.core import config
from awgbot.core import settings
from awgbot.runtime import main as pkg          # подменяемое тестами — через пакет

from . import alive
from .common import log


# Переход имён обвязки 3.3.0 → 3.3.1 — снимается со следующим сдвигом минимума.
_rename_task: "asyncio.Task | None" = None   # прогон обвязки после перехода имён: ссылка держит задачу


async def _plumbing_rename(services) -> None:
    """Прогон обвязки после перехода имён: перенесёт таблицу локальной сети и
    перепишет юнит целиком. Юнит уже запущен извне (загрузка) — тот прогон
    сделает то же сам. Снимается со следующим сдвигом минимума."""
    ok, err = await asyncio.to_thread(services.reassert_guarded, "переход имён обвязки", timeout=600)
    if ok:
        log.info("gateway: обвязка перевыставлена под новые имена")
    else:
        log.warning("gateway: прогон обвязки после перехода имён: %s", err)


async def run_gateway() -> None:
    """Сборка и запуск роли gateway: панель + монитор, больше ничего.

    Диспетчер и роутеры роли — из bot/routers.py, общего со сборкой основного бота;
    остальной старт (БД, сервисы, планировщик) у ролей свой: общий
    «конструктор с ветками» связал бы роли ровно там, где им положено не знать
    друг о друге.
    """
    from awgbot.domain.gateway import GatewayServices
    from awgbot.bot.handlers import gateway as gateway_handlers
    from awgbot.runtime.scheduler import setup_gateway_scheduler
    from awgbot.runtime import preflight

    # первый запуск после установки: базы ещё нет, либо установщик оставил
    # метку (переустановка поверх сохранённых данных базу не трогает)
    marker = os.path.join(os.path.dirname(config.DB_PATH), pkg.FRESH_INSTALL_MARKER)
    fresh = not os.path.exists(config.DB_PATH) or os.path.exists(marker)
    db = pkg.Database(config.DB_PATH)
    db.init_schema()
    services = GatewayServices(db)
    services.first_start_note(fresh)        # «установлен» серверу — только в первый час
    services.adopt_installed_digest()       # digest выпуска от установщика, `awg-bot update` или самообновления — в базу
    services.upd_legacy_drop()              # ключи прежних «⬆️ Обновлений» агента: сам он больше не обновляется
    try:
        services.backup_leftovers_drop()    # своих копий у шлюза нет: фраза, отметки, ключи conf — прочь
    except Exception as e:                               # noqa: BLE001
        log.warning("gateway: следы своих копий не стёрты: %s", e)
    with contextlib.suppress(OSError):
        os.remove(marker)
    # Обвязка прежнего образца после обновления: строки юнита — сразу, до
    # первого снимка; таблица локальной сети — прогоном обвязки в фоне.
    # Снимается со следующим сдвигом минимума.
    try:
        if services.plumbing_rename_due():
            global _rename_task
            _rename_task = asyncio.get_running_loop().create_task(_plumbing_rename(services))
    except Exception as e:                               # noqa: BLE001
        log.warning("gateway: переход имён обвязки не начат: %s", e)

    # Шлюз стоит в юрисдикции, где Telegram заблокирован, и ходит к нему через
    # туннель до ВПС — по метке, как всё помеченное на этой машине. Туннель
    # только IPv4, а резолвер отдаёт api.telegram.org сначала AAAA-адресом:
    # aiohttp полез бы по v6 в никуда и утонул в таймауте ещё до v4-попытки.
    # Прибиваем сессию к IPv4 (tgsession: там же короткий connect и повтор).
    from awgbot.runtime import tgsession
    session = tgsession.make(ipv4_only=settings.get_bool("app.gateway.ipv4_only", True))
    bot = pkg.Bot(config.BOT_TOKEN, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                           link_preview_is_disabled=True))
    # отметка жизни для сторожа обновления: первый ответ на getUpdates или
    # первое подписанное слово сервера AWG в канале (linkclient) — что раньше
    alive.watch(bot)
    token_dead = False
    try:
        me = await bot.get_me()
        # кто этот бот — в снимок канала: сервер ведёт в его чат ссылкой, и
        # токен агента ему для этого не нужен
        services.bot_username = me.username or ""
        services.bot_name = me.first_name or me.username or ""
    except TelegramUnauthorizedError:
        # Токен отозван (утёк — отозвали в BotFather). Падать нельзя: новый
        # токен довезёт только канал до сервера AWG («🔧 Сервис → 🤖 Токен
        # бота»), а он поднимается ниже. Работаем без Telegram: канал, монитор,
        # обвязка; токен приедет — агент перезапустится сам.
        token_dead = True
        log.error("Bot API отверг токен бота шлюза — работаю без Telegram и жду новый токен по "
                  "каналу конфигурации от сервера AWG (или BOT_TOKEN в /etc/awg-bot/env руками и перезапуск)")
    except Exception as e:                               # noqa: BLE001
        log.warning("getMe на старте не прошёл (сеть ещё не готова?): %s — "
                    "продолжаю, polling дождётся сети", e)

    # конец сопряжения (шлюз подключён командой): отметка первого старта —
    # отсюда, а не от первого запуска процесса; терминалу — имя бота
    try:
        services.join_finish_start(getattr(services, "bot_username", "") or "")
    except Exception as e:                               # noqa: BLE001
        log.warning("gateway: конец сопряжения не отмечен: %s", e)

    # пользователей в БД нет: AccessMiddleware пускает админа, остальных молча
    # роняет — ровно то, что шлюзу и нужно; роутеры — bot/routers.py
    dp = pkg.make_dispatcher("gateway", services, db)
    await pkg._menu_command(bot)

    conf_watcher = pkg.ConfWatcher(config.CONF_DIR)
    conf_watcher.start()

    from awgbot.bot import notifier as _notifier

    async def _mail_fallback(text: str) -> None:
        # Telegram шлюза ходит через туннель; лёг линк — Telegram нет, а
        # интернет есть, и письмо уйдёт. Нет и прямого выхода — письмо не
        # уйдёт тем более: не пробуем, чтобы не засорять журнал отказами SMTP.
        if not await asyncio.to_thread(services.email_alert_fallback_enabled):
            return
        if await asyncio.to_thread(services.egress_probe) is None:
            log.warning("Telegram недоступен, и прямого выхода в интернет нет — письмо не уйдёт, не пробую")
            return
        await asyncio.to_thread(services.email_send_alert, text)
    _notifier.set_email_fallback(_mail_fallback)

    try:
        warns = await asyncio.to_thread(preflight.collect_warnings_gateway, services)
        if warns:
            from awgbot.bot.notifier import notify_one
            await notify_one(bot, config.ADMIN_ID, preflight.format_warnings(warns))
    except Exception as e:                               # noqa: BLE001
        log.warning("gateway preflight warnings: %s", e)

    # Итог self-update — общий финишер обеих ролей: убрать «дождись»,
    # отчитаться, единственная живая кнопка «В меню».
    try:
        await pkg.report_update_result(bot, services)
    except Exception as e:                               # noqa: BLE001
        log.warning("gateway confirm_applied_update: %s", e)
    await pkg._announce_zone(bot)                    # ключ пояса снят обновлением — сказать
    # rt-18: очередь общего пула потоков в первые минуты — строкой в журнал
    from awgbot.runtime import poolprobe
    asyncio.create_task(poolprobe.run("gateway: "))

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

    await pkg._announce_reboot(bot, services.db, "агента")
    try:
        if not token_dead and await asyncio.to_thread(services.cfg_token_note_pending):
            from awgbot.bot.notifier import notify_one
            from awgbot.bot import texts as _texts
            if await notify_one(bot, config.ADMIN_ID, _texts.GW_TOKEN_SWITCHED):
                await asyncio.to_thread(services.cfg_token_note_clear)   # не дошло — скажем при следующем старте
                # вопросы из того же документа показывал бы прежний, мёртвый бот
                from awgbot.bot.handlers import gateway as gateway_handlers
                await gateway_handlers.show_cfg_question(bot, services, config.ADMIN_ID)
    except Exception as e:                               # noqa: BLE001
        log.warning("итог смены токена не ушёл: %s", e)

    # Планировщик — СТРОГО после финишера, как у роли основного бота. Его стартовая
    # проверка обновлений укладывается в секунду, и, запущенная раньше, она
    # ставила «доступна следующая» ВЫШЕ «обновлён до…»: последним в чате висел
    # отчёт, а уведомление о следующей ступени терялось над ним.
    scheduler = setup_gateway_scheduler(services, bot)

    # Канал до ВПС внутри линка: включается тем, что
    # привёз бандл. Своего расписания у него нет — сессия висит открытой, а
    # сообщения идут по событию; в простое по каналу не уходит ни байта.
    try:
        from awgbot.runtime import linkclient
        from awgbot.bot.notifier import notify_one as _notify_one

        async def _chan_notify(text: str, *, silent: bool = False) -> None:
            await _notify_one(bot, config.ADMIN_ID, text, silent=silent)
        linkclient.set_notify(_chan_notify)

        async def _chan_edit(chat_id: int, message_id: int, text: str, *, hide: bool = False) -> bool:
            # итог смены ключей и отзыв — правкой самого приглашения; hide — с кнопкой «Скрыть»
            from awgbot.bot import keyboards as _kb
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                            reply_markup=_kb.hide_only() if hide else None)
                return True
            except Exception:                            # noqa: BLE001 — сообщение удалено или старое
                return False
        linkclient.set_edit(_chan_edit)

        async def _chan_ask(section: str) -> None:
            # вопрос с сервера AWG (почта, аплинк, настройки шлюза) — живым меню (первый в очереди)
            from awgbot.bot.handlers import gateway as gateway_handlers
            await gateway_handlers.show_cfg_question(bot, services, config.ADMIN_ID)
        linkclient.set_ask(_chan_ask)
        linkclient.ensure(services)
    except Exception as e:                               # noqa: BLE001
        log.warning("канал линка: клиент не поднят: %s", e)

    log.info("Агент шлюза запущен (роль gateway)")
    try:
        if token_dead:
            # без Telegram: поллинг упал бы на первом же getMe; живут канал и планировщик
            await asyncio.Event().wait()
        await dp.start_polling(bot, polling_timeout=50,
                               allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        conf_watcher.stop()
        from awgbot.runtime import linkclient
        await linkclient.shutdown()
        log.info("Останавливаюсь…")
        with contextlib.suppress(Exception):
            await bot.session.close()
        with contextlib.suppress(Exception):
            db.close()
