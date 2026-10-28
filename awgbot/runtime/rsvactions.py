"""
rsvactions.py — исполнитель действий автоматов резерва (домен возвращает
кортежи, слой runtime делает): `("send", вид, тело)` — по каналу пары,
`("send_released", {gen, blob})` — реплика частями, `("guest", команда,
тело)` — боту основы на резерве по локальному каналу, `("gw", слот, вид,
тело)` — шлюзу основы по гостевому каналу, `("notify", [...])`,
`("fw",)` — перерисовать файервол хоста, `("relay_flush",)` — сбросить
записи conntrack по портам клонов (скрипт зоны `--relay-flush`; стоит после
`("fw",)` с DNAT хвоста, иначе поток между ними завёл бы запись без NAT),
`("live",)` — перерисовать живое
сообщение хода переезда, `("release",)` — остановить поллинг, подтвердить
пачку, опрос вне очереди, финальная реплика, `("restart",)` — перезапуск
бота после слияния, `("take_tg",)` — `deleteWebhook`, поллинг, `taken`.

Контекст (бот, сервисы, управление поллингом) ставит main на старте.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_ctx: dict = {}


def setup(bot, services, pollctl=None) -> None:
    _ctx.update(bot=bot, services=services, pollctl=pollctl)


def services():
    return _ctx.get("services")


async def run(actions) -> None:
    for act in actions or []:
        try:
            await _one(act)
        except Exception as e:                               # noqa: BLE001
            log.warning("резерв: действие %s не выполнено: %s", act[0] if act else act, e)


async def _one(act: tuple) -> None:
    from awgbot.runtime import pairlink
    kind = act[0]
    bot, svc, pc = _ctx.get("bot"), _ctx.get("services"), _ctx.get("pollctl")
    if kind == "send":
        if not await pairlink.send(act[1], act[2] if len(act) > 2 else None):
            log.info("резерв: «%s» не ушло — канала нет", act[1])
    elif kind == "send_released":
        await pairlink.send_released(act[1])
    elif kind == "guest":
        from awgbot.runtime.pairlink import guestctl
        srv = guestctl.server()
        if srv is None or not await srv.send(act[1], act[2] if len(act) > 2 else None):
            log.info("резерв: гостю «%s» не ушло — канала нет", act[1])
    elif kind == "gw":
        from awgbot.runtime.pairlink import guest
        srv = guest.server()
        if srv is None or not await srv.send(int(act[1]), act[2], act[3] if len(act) > 3 else None):
            log.info("резерв: шлюзу %s «%s» не ушло — сессии нет", act[1], act[2])
    elif kind == "notify":
        from awgbot.bot.notifier import send_notifications
        if bot is not None:
            await send_notifications(bot, act[1])
    elif kind == "fw":
        if svc is not None:
            await asyncio.to_thread(svc.reconcile_ssh_access)
    elif kind == "relay_flush":
        from awgbot.infra import rsvzone
        try:
            await asyncio.to_thread(rsvzone.relay_flush)
        except rsvzone.ZoneError as e:
            log.info("резерв: записи conntrack хвоста не сброшены: %s", e)
    elif kind == "live":
        from awgbot.bot.handlers.settings.reserve import move as move_ui
        if bot is not None and svc is not None:
            await move_ui.refresh_live(bot, svc)
    elif kind == "release":
        await _release(bot, svc, pc)
    elif kind == "restart":
        if svc is not None:
            await asyncio.to_thread(svc.restart_bot)
    elif kind == "take_tg":
        if pc is not None and await pc.take():
            await run(await asyncio.to_thread(svc.reserve_tg_taken_mark))
    elif kind == "wake_poll":
        if pc is not None:
            pc.wake()
    elif kind == "take_home":
        if pc is not None and await pc.take():
            await run(await asyncio.to_thread(svc.reserve_stuck_home))
    else:
        log.info("резерв: неизвестное действие %s", kind)


JRN_DRAIN_S = 15


async def _jrn_drain(svc) -> bool:
    """Неотправленные события и счётчики — резерву; ждать его подтверждений (не дольше
    JRN_DRAIN_S). False — не дождались (передача отложится сервисом)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + JRN_DRAIN_S
    while loop.time() < deadline:
        await run(await asyncio.to_thread(svc.reserve_jrn_flush))
        if await asyncio.to_thread(svc.reserve_jrn_all_acked):
            return True
        await asyncio.sleep(0.5)
    return False


async def _release(bot, svc, pc) -> None:
    """Передача бота резерву: поллинг стоп, пачка подтверждена, опрос вне
    очереди (свежие рукопожатия адресатам Т-РК1), финальная реплика → released."""
    from awgbot.runtime import pairlink
    if pc is not None and not await pc.release():
        log.warning("резерв: пачка обновлений не подтверждена — передачу бота откладываю, поллинг обратно")
        await asyncio.to_thread(svc.rsv_update, "rsv_out", step="release")
        pc.wake()
        return
    try:
        await asyncio.to_thread(svc.poll_traffic)
    except Exception as e:                                   # noqa: BLE001
        log.info("резерв: опрос вне очереди перед репликой: %s", e)
    if await asyncio.to_thread(svc.reserve_jrn_on):
        await _jrn_drain(svc)                                # журнал: последние правки и счётчики — до передачи
    built = await asyncio.to_thread(svc.reserve_release_build)
    if not built:
        log.warning("резерв: финальная реплика не собралась — поллинг обратно")
        if pc is not None:
            pc.wake()
        return
    if not await pairlink.send_released(built):
        log.warning("резерв: реплика не ушла — поллинг обратно")
        await asyncio.to_thread(svc.rsv_update, "rsv_out", step="release")
        await asyncio.to_thread(svc.reserve_set_poll, "mine")
        if pc is not None:
            pc.wake()
