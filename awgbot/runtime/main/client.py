"""client.py — часть пакета main (см. __init__): сборка и запуск основной роли (main).
Поллинг Telegram — управляемой задачей (runtime/pollctl.py): с парой серверов право
отвечать держит вебхук, шаг возврата пользователей с резерва добирает его после перезапуска."""

from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from awgbot.core import config
from awgbot.core import settings
from awgbot.runtime import main as pkg          # подменяемое тестами — через пакет

from . import alive
from .common import log


async def main() -> None:
    if pkg.join_pending():
        # шлюз, подключаемый командой: токена бота и ADMIN_ID ещё нет — их
        # довезёт канал после верного кода (runtime/main/join.py)
        settings.init(config.CONF_DIR)
        await pkg.run_join()
        return
    config.validate()
    from awgbot.runtime import preflight
    preflight.check_fatal()                 # стоп-факторы: data-dir, целостность БД
    settings.init(config.CONF_DIR)          # горячий кэш conf/*.yaml (до чтений)
    pkg.log_zone()                          # пояс показа — строкой в журнал (обе роли)

    if config.GUEST:
        # бот основы на резерве: те же данные пользователей, но в хранилище в памяти
        # и в сетевом пространстве зоны — свой старт (runtime/main/guest.py),
        # без всего, что действует на хост
        await pkg.run_guest()
        return
    if config.ROLE == "gateway":
        # Роль агента шлюза — свой мир целиком (docs/ROADMAP.md, п.7): ни
        # пользователей, ни awg-сервера, ни вотчдога конфига. Ветвимся РАНО, а не
        # флажками по всему пути основного бота: пропущенный флажок здесь
        # означал бы «агент полез за awg сервера» — молча и не туда.
        await pkg.run_gateway()
        return

    db = pkg.Database(config.DB_PATH)
    db.init_schema()
    services = pkg.Services(db)
    services.ensure_admin_client()          # админ — тоже пользователь VPN
    services.normalize_update_schedule()    # прежнее never → «месяц» и уведомления выкл
    services.adopt_installed_digest()       # digest выпуска от установщика, `awg-bot update` или самообновления — в базу
    # Сессии канала линка прошлого процесса мертвы вместе с его сокетами: без
    # сброса карточка слота зажгла бы «на связи» у шлюза, который ещё не
    # переподключился. До поллинга — чтобы ни один экран их не увидел.
    services.gwlink_sessions_reset()
    # то же для канала пары резерва: рычаги возврата, пакет и кворум не должны считать
    # второй сервер на связи по сеансу, которого больше нет
    services.reserve_session_reset()
    try:
        # кворум резерва: лидер линии стартует без права записи — до поллинга, не первым тактом
        if hasattr(services, "reserve_quorum_leader_start"):
            services.reserve_quorum_leader_start()
    except Exception as e:                                    # noqa: BLE001
        log.warning("кворум: ограждение на старте: %s", e)

    from awgbot.runtime import tgsession
    bot = pkg.Bot(
        config.BOT_TOKEN,
        session=tgsession.make(),                    # короткий connect и повтор соединения
        # Превью ссылок выключены для всех сообщений: уведомление об обновлении
        # несёт список ссылок на релизы, и карточка первой из них — шум.
        default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                     link_preview_is_disabled=True),
    )
    # отметка жизни для сторожа обновления — первый ответ на getUpdates (база
    # уже открыта); раньше — не в счёт
    alive.watch(bot)
    from awgbot.bot.lists import msgs
    msgs.attach(bot, services)                   # записи сообщений: снимки, «Скрыть», итоги
    # Токен: getMe ловит протухший/отозванный токен сразу (внятное сообщение
    # вместо «молчаливого» бота). ВАЖНО: fatal — ТОЛЬКО явный отказ Bot API
    # (401 Unauthorized). Сетевые ошибки (VPS стартовал раньше сети/DNS) не
    # фатальны: polling ниже ретраится сам, а fatal-цикл здесь исчерпал бы
    # systemd StartLimit и уложил бота там, где надо было подождать 30 секунд.
    try:
        me = await bot.get_me()
        services.bot_username = me.username or ""
        services.bot_name = me.first_name or me.username or ""
    except TelegramUnauthorizedError as e:
        raise preflight.PreflightError(
            f"Bot API отверг токен (getMe: {e}). Проверьте BOT_TOKEN в "
            f"/etc/awg-bot/env и перезапустите.") from e
    except Exception as e:                               # noqa: BLE001
        log.warning("getMe на старте не прошёл (сеть ещё не готова?): %s — "
                    "продолжаю, polling дождётся сети", e)
    # порядок роутеров и AccessMiddleware (outer — до фильтров роутеров) —
    # bot/routers.py, один источник для запуска и тестов
    dp = pkg.make_dispatcher("client", services, db)
    await pkg._menu_command(bot)

    loop = asyncio.get_running_loop()

    def on_change() -> None:
        """Вызывается из потока вотчдога — планируем async-реконсиляцию на loop."""
        asyncio.run_coroutine_threadsafe(pkg.do_reconcile(services, bot), loop)

    watcher = pkg.AwgWatcher(on_change)
    conf_watcher = pkg.ConfWatcher(config.CONF_DIR)   # горячая правка настроек
    conf_watcher.start()
    scheduler = pkg.setup_scheduler(services, bot, db, watcher)
    from awgbot.runtime import pollctl as _pollctl, rsvactions
    pollctl = _pollctl.PollControl(dp, bot, services)
    rsvactions.setup(bot, services, pollctl)
    try:
        # возврат пользователей с резерва на шаге перезапуска: после слияния бот
        # перезапущен, пора забирать Telegram (deleteWebhook) и запись
        if await asyncio.to_thread(services.reserve_return_after_restart):
            log.info("резерв: возврат — после перезапуска забираю Telegram и запись")
    except Exception as e:                               # noqa: BLE001
        log.warning("резерв: шаг возврата после перезапуска: %s", e)

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

    await pkg.do_reconcile(services, bot)                    # сверка состава пиров
    try:
        # восстановить блокировки; после восстановления — и снять лишние DROP,
        # наложенные restore до подъёма интерфейсов по собранной базе
        restored = await asyncio.to_thread(services.restore_done_pending)
        await asyncio.to_thread(services.reconcile_blocks, restored)
    except Exception as e:                               # noqa: BLE001
        log.warning("reconcile_blocks на старте: %s", e)
    # итог self-update: если перед рестартом запускалось обновление — удалить
    # «дождись завершения» и отчитаться админу («успешно обновлен…» + changelog
    # с кнопкой «В меню» / «не применилось»). Флаги стираются однократно.
    try:
        await pkg.report_update_result(bot, services)       # тот же финишер, что у агента
    except Exception as e:                               # noqa: BLE001
        log.warning("report_update_result: %s", e)

    # Поставка привезла ядро нового поколения — переезд профилей обязателен, и
    # сказать об этом надо при КАЖДОМ старте, пока он не начат: пропустить такое
    # сообщение один раз значит остаться на ядре, которое скоро перестанет
    # обслуживать пользователей, и не понять почему.
    try:
        await pkg._notify_migration_needed(bot, services)
    except Exception as e:                               # noqa: BLE001
        log.warning("migration_needed: %s", e)
    # DNS пользователей публичный, а решения ещё нет — предлагать при каждом старте:
    # любой из трёх ответов инфобокс гасит.
    try:
        await pkg._notify_private_dns_offer(bot, services)
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

    # Крючки канала линка — ДО планировщика и слушателя: агент, подключившийся
    # во время старта, шлёт installed сразу, и без крючка «шлюз настроен»
    # терялось бы навсегда (повтора не будет, файл с ключом остаётся в чате).
    try:
        from awgbot.runtime import linkserver as _linkserver
        from awgbot.bot.handlers.settings import bundle_installed

        async def _on_installed(slot_id: int) -> None:
            await bundle_installed(bot, services, slot_id)
        _linkserver.set_on_installed(_on_installed)

        # сообщение подключения шлюза командой: подключился, срок, готово
        from awgbot.bot.handlers.settings import gwjoin_set_hooks
        gwjoin_set_hooks(bot, services)

        async def _on_notes(notes) -> None:
            # события смены ключей линка: «сменены», «отклонили», «вернул прежние»
            await pkg.send_notifications(bot, notes)
        _linkserver.set_on_notes(_on_notes)

        # канал пары серверов (резерв): уведомления домена и события сопряжения в чат
        from awgbot.runtime import pairlink as _pairlink
        from awgbot.bot.handlers.settings.reserve import events as _rsv_events
        _pairlink.set_on_notes(_on_notes)
        _pairlink.set_on_event(_rsv_events.make_on_event(bot, services))

        async def _on_rekeyed(slot_id: int) -> None:
            # прежний файл конфигурации слота несёт старые ключи линка — из чата прочь
            from awgbot.bot.handlers.settings.render import _drop_bundle_msgs
            await _drop_bundle_msgs(bot, services, slot_id)
        _linkserver.set_on_rekeyed(_on_rekeyed)

        async def _on_restore(slot_id: int, blob: bytes, name: str) -> str:
            # бэкап сервера из чата бота шлюза: штатные рельсы — вопрос в чате
            # админа, его данные — в том же хранилище диалогов, что у файла из чата
            from aiogram.fsm.context import FSMContext
            from aiogram.fsm.storage.base import StorageKey
            from awgbot.bot.handlers.restore import relay_restore
            state = FSMContext(storage=dp.storage, key=StorageKey(bot_id=bot.id, chat_id=config.ADMIN_ID,
                                                                  user_id=config.ADMIN_ID))
            return await relay_restore(bot, services, state, blob, name, slot_id)
        _linkserver.set_on_restore(_on_restore)

        async def _on_fleet(services_, out) -> None:
            # набор обновлений: живое сообщение хода и уведомления (догон, откат сервера)
            from awgbot.bot.handlers.fleet import fleet_show
            await fleet_show(bot, services_, out)
        _linkserver.set_on_fleet(_on_fleet)
    except Exception as e:                               # noqa: BLE001
        log.warning("канал линка: крючки не поставлены: %s", e)

    watcher.ensure_watching()
    log.info("Бот запущен")

    # ── после старта поллинга: всё, без чего первый апдейт обслуживается ──
    # Раньше это шло до getUpdates и стоило 10–30 с молчания на каждом старте:
    # списки маршрутизации (HTTP, до 15 с на источник), зонд шлюза и вход по
    # IMAP в замечаниях, четыре запроса к Bot API за именем. Бот отвечает через
    # пару секунд, а это догоняет в фоне; сбой любого шага — в лог, не наружу.
    # Планировщик стартует ОТСЮДА, после обязательных шагов (SSH из туннеля,
    # юниты слотов): раньше он шёл параллельно и первые тики делали ту же
    # работу дважды, а слушатель канала биндился до миграции юнитов.
    # Списки и реконсиляцию маршрутизации делает первый тик монитора сам.
    async def _after_start() -> None:
        try:
            await asyncio.to_thread(services.reconcile_ssh_access)  # SSH из туннеля: set устройств админа
            await asyncio.to_thread(services.retire_legacy_ssh_gate)
        except Exception as e:                           # noqa: BLE001
            log.warning("reconcile_ssh_access на старте: %s", e)
        try:
            # override юнита dnsmasq — к версии из поставки: режим install при
            # обновлении не повторяется, и правки override'а иначе не доезжают
            await asyncio.to_thread(services.resolver_ensure_dropin)
        except Exception as e:                           # noqa: BLE001
            log.warning("resolver_ensure_dropin на старте: %s", e)
        try:
            # резолвер админа («SMB в туннеле»): живые хосты установщик заново не
            # проходят — ставит бот; записи — тумблер мог быть включён до рестарта
            await asyncio.to_thread(services.admin_dns_ensure)
        except Exception as e:                           # noqa: BLE001
            log.warning("admin_dns_ensure на старте: %s", e)
        try:
            # Слоты шлюзов: юнит первого линка —
            # на шаблон, активный на холодном старте — предпочтительный.
            await asyncio.to_thread(services.gateway_units_migrate)
            # линки слотов с конфигом и без юнита (бэкап на пустом сервере)
            await asyncio.to_thread(services.gateway_links_adopt)
            # порт линка правят руками в конфиге ВПС — строка слота узнаёт сама
            for note in await asyncio.to_thread(services.gateway_sync_link_ports):
                await pkg.notify_one(bot, note.tg_id, note.text)
            await asyncio.to_thread(services.routing_cold_start)
        except Exception as e:                           # noqa: BLE001
            log.warning("слоты шлюзов на старте: %s", e)
        try:
            # адреса в туннеле локальных сетей шлюзов: таблица — до первого такта
            await asyncio.to_thread(services.lan_map_startup)
        except Exception as e:                           # noqa: BLE001
            log.warning("адреса в туннеле на старте: %s", e)
        try:
            scheduler.start()
        except Exception as e:                           # noqa: BLE001
            log.warning("планировщик не запущен: %s", e)
        # rt-18: очередь общего пула потоков в первые минуты — строкой в журнал
        from awgbot.runtime import poolprobe
        asyncio.create_task(poolprobe.run())
        try:
            await pkg._sync_bot_identity(bot, db)
        except Exception as e:                           # noqa: BLE001
            log.warning("set_my_name/description/delete_commands: %s", e)
        # Замечания preflight — первым содержательным сообщением админу. Через
        # notifier: он и тихие часы соблюдает, и подставляет «Скрыть» — это
        # проактивное уведомление, админ его не заказывал.
        try:
            warns = await asyncio.to_thread(preflight.collect_warnings, services, startup_ok)
            if warns:
                await pkg.notify_one(bot, config.ADMIN_ID, preflight.format_warnings(warns))
        except Exception as e:                           # noqa: BLE001
            log.warning("preflight warnings: %s", e)
        try:
            # отчёт о восстановлении — до слушателя канала: ожидание выхода
            # шлюзов на связь должно стоять раньше их первого hello
            from awgbot.bot.handlers.restore import report_restore_result
            await report_restore_result(bot, services)
        except Exception as e:                           # noqa: BLE001
            log.warning("report_restore_result: %s", e)
        try:
            # Слушатель канала линка — после слотов: он биндится на адреса их
            # /30, и до миграции юнитов их могло не быть.
            from awgbot.runtime import linkserver
            await linkserver.ensure(services)
        except Exception as e:                           # noqa: BLE001
            log.warning("канал линка: слушатель не поднят: %s", e)
        try:
            # канал пары серверов (резерв): слушатель у пригласившего, клиент у принявшего;
            # крючок коммита базы — отметка пакета переезда; на старте пакет уходит заново
            from awgbot.runtime import pairlink
            services.reserve_pkg_hook_install()
            if services.reserve_paired():
                services.reserve_pkg_mark()
            await pairlink.ensure(services)
            from awgbot.runtime.pairlink import guestctl
            await guestctl.ensure(services)
        except Exception as e:                           # noqa: BLE001
            log.warning("канал пары: не поднят: %s", e)
        try:
            # набор обновлений: откат сервера сторожем, ход и откат — с записанного
            from awgbot.runtime.linkserver import upd as _fleet_upd
            await _fleet_upd.on_start(services)
        except Exception as e:                           # noqa: BLE001
            log.warning("набор обновлений на старте: %s", e)
        try:
            # имена ботов шлюзов для ссылок в карточках — только тем слотам,
            # у которых ответа ещё нет
            from awgbot.runtime import gwbotme
            await gwbotme.ensure_all(services)
        except Exception as e:                           # noqa: BLE001
            log.warning("боты шлюзов: getMe: %s", e)
        await pkg._announce_reboot(bot, db, "бота")
        log.info("стартовые фоновые задачи завершены")

    after_start = asyncio.create_task(_after_start())

    def _after_start_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:                              # rlogic-15: не глотать молча
            log.warning("стартовые фоновые задачи прервались: %r", exc)
    after_start.add_done_callback(_after_start_done)

    try:
        # Поллинг — управляемой задачей (runtime/pollctl): long-poll 50 с вместо
        # дефолтных 10 (впятеро меньше холостых getUpdates), allowed_updates
        # ЯВНО из зарегистрированных типов (иначе getUpdates мог не запросить
        # message с deep-link /start). С парой серверов право отвечать держит
        # вебхук: чужой адрес в getWebhookInfo или 409 на getUpdates — поллинга
        # нет, пока пользователи не вернутся (deleteWebhook — шаг возврата).
        await pollctl.run()
    finally:
        log.info("Останавливаюсь…")
        scheduler.shutdown(wait=False)
        watcher.stop()
        conf_watcher.stop()
        from awgbot.runtime import linkserver, pairlink
        from awgbot.runtime.pairlink import guestctl
        await pollctl.close()
        await linkserver.shutdown()
        await pairlink.shutdown()
        await guestctl.shutdown()
        await bot.session.close()
        db.close()
