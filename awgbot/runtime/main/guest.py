"""guest.py — часть пакета main (см. __init__): запуск бота основы на резерве
(гостевой режим, run_guest).

Тот же основной бот с данными основы из хранилища гостя в памяти (env, conf,
база — их кладёт процесс открытия) в сетевом пространстве зоны: клон
интерфейса основы, dnsmasq зоны, veth к хосту. Что пропускается против
обычного старта: сторож обновления (`alive.watch` — он у резерва свой),
сопряжения шлюзов, имя бота в документ шлюзов, уведомления о переезде
профилей и своём резолвере, слушатель канала шлюзов, набор обновлений,
бэкапы, вотчдог конфига и файла интерфейса. Сверка пиров и блокировок — как
есть, по клону. Вместо поллинга — слушатель вебхука (`runtime/webhook`):
Telegram отдаёт обновления сюда после `setWebhook`, который ставится по
факту записи DNS (отметка `tg_take` от процесса открытия или команда бота
резерва по локальному каналу). Остановка — по команде `stop` того же канала
(возврат) или сигналу: финальный снимок, реплика боту резерва, выход.

С журналом (база открыта процессом с отметкой `capture`): такт журнала раз в
секунду — свои записи основе, догон основы до передачи бота (эпоха взята —
ставить вебхук); снимков `rep.bin` нет — журнал данных гостя на диске; на
возврат вместо реплики — последние записи, ожидание подтверждений основы
(не дольше JRN_DRAIN_S), следование и `released{epoch, last}`.
"""
from __future__ import annotations

import asyncio
import contextlib

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from awgbot.core import config
from awgbot.runtime import main as pkg          # подменяемое тестами — через пакет

from .common import log

MOVE_NOTIFY_DELAY_S = 60          # TTL записи: переподключение раньше попало бы на прежний адрес из кэша
JRN_DRAIN_S = 20                  # возврат: подтверждения основы на последние записи до released
JRN_AFTER_S = 600                 # после released — досылать неподтверждённое, пока основа не возьмёт всё


async def run_guest() -> None:
    from awgbot.infra import rsvzone
    from awgbot.runtime import webhook
    from awgbot.runtime.pairlink.guestctl import GuestCtlClient
    from awgbot.runtime.scheduler import setup_guest_scheduler
    db = pkg.Database(config.DB_PATH)
    db.init_schema()
    services = pkg.Services(db)
    if services.guest_adopt_snapshot():
        # база подменена под открытым соединением — открыть заново
        db.close()
        db = pkg.Database(config.DB_PATH)
        db.init_schema()
        services = pkg.Services(db)
    missing = services.guest_install_refusals()
    if missing:
        log.warning("гость: отказы не поставлены на %s", ", ".join(missing))
    services.guest_snapshot_hook_install()
    services.ensure_admin_client()
    services.gwlink_sessions_reset()
    from awgbot.runtime import tgsession
    bot = pkg.Bot(config.BOT_TOKEN, session=tgsession.make(),
                  default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    from awgbot.bot.lists import msgs
    msgs.attach(bot, services)                       # записи сообщений — в базе зоны
    try:
        me = await bot.get_me()
        services.bot_username = me.username or ""
        services.bot_name = me.first_name or me.username or ""
    except TelegramUnauthorizedError as e:
        from awgbot.runtime import preflight
        raise preflight.PreflightError(f"Bot API отверг токен основы (getMe: {e})") from e
    except Exception as e:                               # noqa: BLE001
        log.warning("гость: getMe на старте не прошёл: %s — продолжаю", e)
    dp = pkg.make_dispatcher("guest", services, db)
    await pkg._menu_command(bot)
    # сверка пиров клона и блокировок в зоне — как у основы на старте
    await asyncio.to_thread(services.detect_and_handle_restart)
    try:
        n = await asyncio.to_thread(services.db.normalize_default_iface, config.AWG_INTERFACE)
        if n:
            log.info("гость: нормализовано устройств: %d (iface → дефолт)", n)
    except Exception as e:                               # noqa: BLE001
        log.warning("гость: normalize_default_iface: %s", e)
    await pkg.do_reconcile(services, bot)
    ctl = GuestCtlClient(services)
    ctl.start()
    from awgbot.runtime.pairlink import guestctl as _guestctl
    _guestctl.set_client(ctl)
    wh_conf = webhook.load_conf()
    wh = webhook.WebhookServer(wh_conf, webhook.load_pool()) if wh_conf is not None else None
    if wh is None:
        log.error("гость: настройки вебхука не прочитались — право отвечать в Telegram не возьму")
    tg_set = asyncio.Event()
    watch_due = 0.0
    notify_at = {"t": 0.0}
    told: dict = {}

    async def ctl_tick() -> None:
        """Такт локального канала: число устройств на связи, отметка «ставить вебхук»."""
        nonlocal watch_due
        if ctl.connected():
            await ctl.send_online()
            addrs = await asyncio.to_thread(services.guest_blocks_to_send)
            if addrs is not None and await ctl.send("blocks", {"addrs": addrs}):
                await asyncio.to_thread(services.guest_blocks_sent, addrs)
        if wh is not None:
            try:
                wh.refresh_pool()                         # хост выучил сеть Telegram — в фильтр без перезапуска
            except Exception as e:                        # noqa: BLE001
                log.info("гость: пул вебхука не перечитан: %s", e)
        if not tg_set.is_set() and wh is not None and (ctl.take_tg.is_set() or rsvzone.TG_TAKE_FILE.exists()) \
                and await asyncio.to_thread(services.guest_can_take_tg):   # с журналом — только держателем
            if await wh.set_webhook(bot, dp):
                tg_set.set()
                watch_due = asyncio.get_running_loop().time() + webhook.watch_delay()
                await ctl.send("tg_taken")
                ttl = int(rsvzone.read_json(rsvzone.RUN_DIR / "pkg.json").get("ttl") or 0)
                notify_at["t"] = asyncio.get_running_loop().time() + max(MOVE_NOTIFY_DELAY_S, ttl)   # не раньше TTL записи
        if tg_set.is_set() and not ctl.tg_acked.is_set() and ctl.connected():
            await ctl.send("tg_taken")                    # канал мог быть не подключён в момент вебхука — повтор до ack
        if tg_set.is_set() and notify_at["t"] and asyncio.get_running_loop().time() >= notify_at["t"]:
            notify_at["t"] = 0.0
            try:
                from awgbot.bot.notifier import send_notifications
                rf = str(services.guest_state().get("reason") or "") == "rf"
                await send_notifications(bot, await asyncio.to_thread(services.guest_move_notify, rf))
            except Exception as e:                       # noqa: BLE001
                log.warning("гость: «переподключи VPN» не разослано: %s", e)
        await _primary_back_notice(bot, services, told)
        if tg_set.is_set() and wh is not None and asyncio.get_running_loop().time() >= watch_due:
            watch_due = asyncio.get_running_loop().time() + webhook.watch_delay()
            await _watch(wh, bot, services, ctl)

    async def jrn_loop() -> None:
        """Такт журнала гостя: записи основе, эпоха после передачи бота — вебхук."""
        while True:
            await asyncio.sleep(1)
            try:
                for kind, body in await asyncio.to_thread(services.guest_jrn_tick):
                    await ctl.send(kind, body)
                if await asyncio.to_thread(services.guest_jrn_take_tg):
                    ctl.take_tg.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # noqa: BLE001
                log.warning("гость: такт журнала: %s", e)

    jrn_task = asyncio.get_running_loop().create_task(jrn_loop()) if services.guest_jrn_on() else None
    scheduler = setup_guest_scheduler(services, bot, db, on_ctl_tick=ctl_tick)
    if wh is not None:
        try:
            await wh.start(dp, bot)
        except Exception as e:                           # noqa: BLE001
            log.error("гость: слушатель вебхука не поднялся: %s", e)
            wh = None
    scheduler.start()
    log.info("гость: бот основы на резерве запущен (клон %s, данные в памяти)", config.AWG_INTERFACE)
    try:
        await ctl.stop_req.wait()
        journal = services.guest_jrn_on()
        log.info("гость: команда возврата — %s", "последние записи журнала основе" if journal
                 else "финальный снимок и реплика боту резерва")
        try:
            if wh is not None:
                await wh.stop()                          # слушатель закрыт: Telegram копит обновления у себя
                wh = None
            try:
                await asyncio.to_thread(services.poll_traffic)   # свежие счётчики и рукопожатия в реплику
            except Exception as e:                       # noqa: BLE001
                log.info("гость: опрос перед репликой: %s", e)
            if journal:
                await _jrn_release(services, ctl, jrn_task)
                return
            blob = await asyncio.to_thread(services.guest_snapshot_bytes)
            await asyncio.to_thread(services.guest_snapshot_tick, True)
            await ctl.send_replica(blob)
            await ctl.send("bye")
        except Exception as e:                           # noqa: BLE001
            log.warning("гость: финальная реплика не ушла: %s", e)
    finally:
        log.info("гость: останавливаюсь…")
        if jrn_task is not None:
            jrn_task.cancel()
        if not ctl.stop_req.is_set() and not services.guest_jrn_on():
            # остановка не по команде (сигнал юнита, перезапуск): последние правки — в rep.bin
            try:
                await asyncio.to_thread(services.guest_snapshot_tick, True)
            except Exception as e:                       # noqa: BLE001
                log.info("гость: финальный снимок при остановке: %s", e)
        scheduler.shutdown(wait=False)
        if wh is not None:
            await wh.stop()
        await ctl.stop()
        with contextlib.suppress(Exception):
            await bot.session.close()
        db.close()


async def _jrn_release(services, ctl, jrn_task) -> None:
    """Возврат с журналом: последние записи, ожидание подтверждений основы, следование
    и `released{epoch, last}` боту резерва; не дождались — свои события остаются в базе
    (`jrn_pass(keep_own=True)`, не хвост): основа просит их `jneed`, досылка — до JRN_AFTER_S."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + JRN_DRAIN_S
    while loop.time() < deadline:
        for kind, body in await asyncio.to_thread(services.guest_jrn_tick):
            await ctl.send(kind, body)
        if await asyncio.to_thread(services.guest_jrn_drained):
            break
        await asyncio.sleep(0.5)
    else:
        log.warning("гость: основа не подтвердила последние записи за %d с", JRN_DRAIN_S)
    if jrn_task is not None:
        jrn_task.cancel()
    body = await asyncio.to_thread(services.guest_jrn_release)
    await ctl.send("released", body)
    log.info("гость: передал право записи по %s (эпоха %s)", body.get("last"), body.get("epoch"))
    # основа не берёт эпоху без догона: неподтверждённое — досылать (её jneed приходит через бот
    # резерва), пока не подтвердит всё; бот резерва снимет зону после её `back`
    deadline = loop.time() + JRN_AFTER_S
    while loop.time() < deadline and not await asyncio.to_thread(services.guest_jrn_drained):
        for kind, b in await asyncio.to_thread(services.guest_jrn_tick):
            await ctl.send(kind, b)
        await asyncio.sleep(1)
    await ctl.send("bye")


async def _primary_back_notice(bot, services, told: dict) -> None:
    """Т-РУ6: основа снова в строю — «возвращаю» (рычаг «сразу») или «верни кнопкой»; один раз."""
    st = services.guest_state()
    pr = st.get("primary") or {}
    key = "button" if pr.get("can_return") else ("auto" if pr.get("healthy") and st.get("return") == "immediate" else "")
    if not key or told.get(key):
        return
    told[key] = True
    from awgbot.bot.notifier import notify_one
    from awgbot.domain.notices import reserve as rn
    await notify_one(bot, config.ADMIN_ID, rn.primary_back(auto=(key == "auto")))


async def _watch(wh, bot, services, ctl) -> None:
    """Надзор вебхука: застой доставки → Т-РУ19 админу основы и строка в журнал."""
    res = await wh.watch(bot)
    if not res:
        return
    if res.get("foreign"):
        log.warning("гость: вебхук указывает не сюда (%s) — право отвечать не у меня", res.get("url"))
        return
    from awgbot.bot.notifier import notify_one
    from awgbot.bot.texts.reserve import guest as gt
    name = str(services.guest_state().get("name") or "резерв")
    if res.get("stalled"):
        log.warning("гость: Telegram не доставляет обновления: %s", res.get("error"))
        await notify_one(bot, config.ADMIN_ID, gt.webhook_stalled(name, res.get("since") or "", res.get("error") or ""))
        await ctl.send("webhook", {"stalled": True, "since": str(res.get("since") or ""), "error": str(res.get("error") or "")[:200]})
    elif res.get("restored"):
        await notify_one(bot, config.ADMIN_ID, gt.webhook_restored(name))
        await ctl.send("webhook", {"stalled": False})
