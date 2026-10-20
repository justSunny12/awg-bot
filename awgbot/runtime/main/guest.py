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
    bot = pkg.Bot(config.BOT_TOKEN,
                  default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
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

    async def ctl_tick() -> None:
        """Такт локального канала: число устройств на связи, отметка «ставить вебхук»."""
        nonlocal watch_due
        if ctl.connected():
            await ctl.send_online()
        if not tg_set.is_set() and wh is not None and (ctl.take_tg.is_set() or rsvzone.TG_TAKE_FILE.exists()):
            if await wh.set_webhook(bot, dp):
                tg_set.set()
                watch_due = asyncio.get_running_loop().time() + webhook.watch_delay()
                await ctl.send("tg_taken")
                notify_at["t"] = asyncio.get_running_loop().time() + MOVE_NOTIFY_DELAY_S
        if tg_set.is_set() and notify_at["t"] and asyncio.get_running_loop().time() >= notify_at["t"]:
            notify_at["t"] = 0.0
            try:
                from awgbot.bot.notifier import send_notifications
                rf = str(services.guest_state().get("reason") or "") == "rf"
                await send_notifications(bot, await asyncio.to_thread(services.guest_move_notify, rf))
            except Exception as e:                       # noqa: BLE001
                log.warning("гость: «переподключи VPN» не разослано: %s", e)
        if tg_set.is_set() and wh is not None and asyncio.get_running_loop().time() >= watch_due:
            watch_due = asyncio.get_running_loop().time() + webhook.watch_delay()
            await _watch(wh, bot, services, ctl)

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
        log.info("гость: команда возврата — финальный снимок и реплика боту резерва")
        try:
            if wh is not None:
                await wh.stop()                          # слушатель закрыт: Telegram копит обновления у себя
                wh = None
            try:
                await asyncio.to_thread(services.poll_traffic)   # свежие счётчики и рукопожатия в реплику
            except Exception as e:                       # noqa: BLE001
                log.info("гость: опрос перед репликой: %s", e)
            blob = await asyncio.to_thread(services.guest_snapshot_bytes)
            await asyncio.to_thread(services.guest_snapshot_tick, True)
            await ctl.send_replica(blob)
            await ctl.send("bye")
        except Exception as e:                           # noqa: BLE001
            log.warning("гость: финальная реплика не ушла: %s", e)
    finally:
        log.info("гость: останавливаюсь…")
        scheduler.shutdown(wait=False)
        if wh is not None:
            await wh.stop()
        await ctl.stop()
        with contextlib.suppress(Exception):
            await bot.session.close()
        db.close()


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
    elif res.get("restored"):
        await notify_one(bot, config.ADMIN_ID, gt.webhook_restored(name))
