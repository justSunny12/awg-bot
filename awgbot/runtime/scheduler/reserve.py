"""reserve.py — задания резерва в планировщике основного бота: живость линка
пары (такт 5 с, локально, без сети: счётчик линка; проба по каналу — только
по подозрению), срок приглашения (раз в минуту, локально), подъём канала пары
по смене роли. Задания следующих этапов (пакет, голоса, надзор) — здесь же."""
from __future__ import annotations

import asyncio
import logging

from apscheduler.triggers.interval import IntervalTrigger

from awgbot.core import config

log = logging.getLogger(__name__)


def setup_reserve_jobs(scheduler, services, bot) -> None:
    from awgbot.bot.notifier import send_notifications

    async def job_liveness():
        try:
            act, notes = await asyncio.to_thread(services.reserve_liveness_tick)
            if hasattr(services, "reserve_pair_confirm_tick"):
                notes = list(notes) + list(await asyncio.to_thread(services.reserve_pair_confirm_tick))
            if notes:
                await send_notifications(bot, notes)
            if act == "ping":
                from awgbot.runtime import pairlink
                await pairlink.send("ping", {"n": services.reserve_ping_nonce()})
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: такт живости: %s", e)
        try:
            # `st` второй стороне по смене тела (направления, здоровье, рычаг, пакет) — не только в начале сессии
            from awgbot.runtime import pairlink
            if pairlink.online():
                body = await asyncio.to_thread(services.reserve_st_if_changed)
                if body is not None and not await pairlink.send("st", body):
                    await asyncio.to_thread(services.reserve_st_sent_reset)
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: st по смене: %s", e)
        try:
            # голосование: живость шлюзов основы по сторонам зоны, матрица случаев,
            # демпфер случаев 3 и Н3, просьбы основе — действия исполняет rsvactions
            from awgbot.runtime import rsvactions
            await rsvactions.run(await asyncio.to_thread(services.reserve_vote_tick))
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: такт голосования: %s", e)

    scheduler.add_job(job_liveness, IntervalTrigger(seconds=5, timezone=config.TZ),
                      id="rsv_liveness", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    async def job_invite_tick():
        try:
            st = await asyncio.to_thread(services.reserve_invite_tick)
            if st:
                from awgbot.runtime import pairlink
                await pairlink.pair_close("expired", notify=True)
            from awgbot.runtime import pairlink
            await pairlink.ensure(services)
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: такт приглашения: %s", e)
        try:
            # новый аварийный код после снятия или замены шлюза — админу, со звуком, удаляется через 10 мин
            why = await asyncio.to_thread(services.reserve_code_notify_pending)
            if why:
                from awgbot.bot.handlers.settings.reserve.events import send_code
                await send_code(bot, services, renewed_why=why)
                await asyncio.to_thread(services.reserve_code_notify_clear)
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: новый код не отправлен: %s", e)
        try:
            # постоянные линки шлюзов: ключи шлюзовых сторон резерву (slots), пока
            # у всех слотов нет стороны зоны; слушатель гостевого канала в зоне;
            # Т-РУ18 «шлюз не на связи с резервом»
            from awgbot.runtime import pairlink, rsvactions
            from awgbot.runtime.pairlink import guest
            await asyncio.to_thread(services.reserve_zone_shell_ensure)
            body = await asyncio.to_thread(services.reserve_slots_body)
            if body and pairlink.online() and await pairlink.send("slots", body):
                await asyncio.to_thread(services.reserve_slots_sent, body)
            await guest.ensure(services)
            notes = await asyncio.to_thread(services.reserve_gw_links_tick)
            if notes:
                await rsvactions.run([("notify", notes)])
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: линки шлюзов: %s", e)
        try:
            # локальный канал с ботом основы на резерве: слушатель — пока есть
            # пара; состояние основы гостю — по смене
            from awgbot.runtime.pairlink import guestctl
            srv = await guestctl.ensure(services)
            if srv is not None and srv.connected():
                await srv.push_state()
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: локальный канал гостя: %s", e)

    from awgbot.util import timeutil as _tu
    scheduler.add_job(job_invite_tick, IntervalTrigger(minutes=1, timezone=config.TZ),
                      id="rsv_invite_tick", max_instances=1, coalesce=True, next_run_time=_tu.now(),
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    # пакет переезда: по отметке (≥ 10 с с последней записи) и по таймеру с джиттером —
    # решает сервис, такт лишь спрашивает; уходит частями по каналу пары
    async def job_pkg_tick():
        try:
            await asyncio.to_thread(services.reserve_pkg_sending_stale)
            built = await asyncio.to_thread(services.reserve_pkg_tick)
            if built:
                from awgbot.runtime import pairlink
                if not await pairlink.send_package(built):
                    await asyncio.to_thread(services.reserve_pkg_bad, int(built["gen"]), "сессия оборвалась")
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: такт пакета: %s", e)

    scheduler.add_job(job_pkg_tick, IntervalTrigger(seconds=5, timezone=config.TZ),
                      id="rsv_pkg_tick", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    # переезд и возврат: автоматы обоих направлений, слово гостя по локальному
    # каналу (вебхук поставлен, реплика, кнопка возврата), блокировки основы
    # резерву — действия исполняет runtime/rsvactions
    async def job_move_tick():
        from awgbot.runtime import rsvactions
        from awgbot.runtime.pairlink import guestctl
        try:
            await rsvactions.run(await asyncio.to_thread(services.reserve_out_tick))
            await rsvactions.run(await asyncio.to_thread(services.reserve_in_tick))
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: такт переезда: %s", e)
        try:
            srv = guestctl.server()
            if srv is not None:
                for word in srv.take_words():
                    if word[0] == "tg_taken":
                        await rsvactions.run(await asyncio.to_thread(services.reserve_guest_tg_taken))
                    elif word[0] == "replica":
                        await rsvactions.run(await asyncio.to_thread(services.reserve_guest_replica_in, word[1]))
                    elif word[0] == "back_req":
                        await rsvactions.run(await asyncio.to_thread(services.reserve_guest_back_req))
                    elif word[0] == "webhook":
                        await rsvactions.run(await asyncio.to_thread(services.reserve_guest_webhook_in, word[1]))
                await asyncio.to_thread(services.reserve_guest_online_set, srv.online_count if srv.connected() else 0)
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: слова гостя: %s", e)
        try:
            addrs = await asyncio.to_thread(services.reserve_blocks_to_send)
            if addrs is not None:
                await rsvactions.run([("send", "blocks", {"addrs": addrs})])
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: блокировки резерву: %s", e)

    scheduler.add_job(job_move_tick, IntervalTrigger(seconds=3, timezone=config.TZ),
                      id="rsv_move_tick", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)

    # надзор за записью (сам решает, пора ли: 2 мин ± 40 %), Т-РК2 через 10 мин
    # после возврата, итог трафика клона раз в минуту
    async def job_slow_tick():
        from awgbot.runtime import rsvactions
        for fn, what in ((services.reserve_record_watch_tick, "надзор записи"),
                         (services.reserve_return_notify_tick, "уведомление о возврате")):
            try:
                notes = await asyncio.to_thread(fn)
                if notes:
                    await rsvactions.run([("notify", notes)])
            except Exception as e:                       # noqa: BLE001
                log.warning("резерв: %s: %s", what, e)
        try:
            await asyncio.to_thread(services.reserve_traffic_in_tick)
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: учёт клона: %s", e)
        try:
            # пул адресов Telegram: обучение по SYN (только serving) и анонсы AS
            # Telegram (сам решает, пора ли: tg_pool_hours ± 40 %)
            await rsvactions.run(await asyncio.to_thread(services.reserve_tg_learn_tick))
            notes = await asyncio.to_thread(services.reserve_tg_pool_tick)
            if notes:
                await rsvactions.run([("notify", notes)])
        except Exception as e:                           # noqa: BLE001
            log.warning("резерв: пул адресов Telegram: %s", e)

    scheduler.add_job(job_slow_tick, IntervalTrigger(seconds=60, jitter=15, timezone=config.TZ),
                      id="rsv_slow_tick", max_instances=1, coalesce=True,
                      misfire_grace_time=config.MISFIRE_GRACE_INTERVAL_SECONDS)
