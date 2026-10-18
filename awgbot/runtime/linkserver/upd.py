"""upd.py — набор обновлений на стороне канала: такт набора, ответ шлюза
`upd_ack`, hello шлюза, старт бота.

Решает домен (domain/fleet: что отправить, что сделать серверу); здесь —
отправка шлюзам на живые сессии, перерисовка живого сообщения и уведомления
крючком main, затем действие сервера над собой (оно может перезапустить
процесс — поэтому последним). Один такт за раз.
"""
from __future__ import annotations

import asyncio
import logging

from awgbot.runtime import linkserver as pkg          # слушатель и крючки — на пакете: их подменяют тесты

log = logging.getLogger(__name__)

_LOCK = asyncio.Lock()
_on_fleet = None


def set_on_fleet(fn) -> None:
    """Крючок main (async fn(services, out)): живое сообщение набора и
    уведомления — у слушателя канала своего бота нет."""
    global _on_fleet
    _on_fleet = fn


def _fleet(services) -> bool:
    return callable(getattr(services, "fleet_tick", None))


async def _run(services, out, srv=None) -> None:
    for _ in range(8):                                    # отказ ступени — откат следующим тактом
        srv = srv or pkg.current()
        for slot, what, body in out.send:
            if srv is None or not await _send(srv, slot, what, body):
                log.info("набор: слоту %s «%s» не ушло — повторю по hello", slot, what)
                continue
            if what in ("upd_commit", "upd_revert"):
                await asyncio.to_thread(services.fleet_delivered, slot, what, body)
        if (out.live or out.notes) and _on_fleet is not None:
            try:
                await _on_fleet(services, out)
            except Exception as e:                        # noqa: BLE001
                log.warning("набор: живое сообщение не обновлено: %s", e)
        if not out.server:
            return
        out = await asyncio.to_thread(services.fleet_server_act, out)


async def _send(srv, slot: int, what: str, body: dict) -> bool:
    """Виды набора — по имени: в канал уходят только они (`upd`, `upd_commit`,
    `upd_revert`), а не любое имя от домена."""
    if what == "upd":
        return await srv.send(slot, "upd", body)
    if what == "upd_commit":
        return await srv.send(slot, "upd_commit", body)
    if what == "upd_revert":
        return await srv.send(slot, "upd_revert", body)
    log.warning("набор: неизвестный вид «%s» слоту %s — не отправлен", what, slot)
    return False


async def fleet_run_tick(services) -> None:
    """Такт набора (такт живости, кнопка, старт)."""
    if not _fleet(services):
        return
    async with _LOCK:
        try:
            out = await asyncio.to_thread(services.fleet_tick)
            await _run(services, out)
        except Exception as e:                            # noqa: BLE001
            log.warning("набор: такт не прошёл: %s", e)


async def fleet_job(services) -> None:
    """Такт набора по расписанию (своё задание, без условия маршрутизации:
    ход сервера без шлюзов тоже ждёт факта по такту). Без хода и без дел у
    шлюзов — выход на первой проверке: вне хода всё решают hello и ответы."""
    if not _fleet(services):
        return
    try:
        if not await asyncio.to_thread(services.fleet_needs_tick):
            return
    except Exception as e:                                # noqa: BLE001
        log.warning("набор: такт по расписанию не прошёл: %s", e)
        return
    await fleet_run_tick(services)


async def on_hello(services, slot_id: int, srv=None) -> None:
    """Шлюз вышел на связь: удержание прошлого хода, догон, следование, очередь.
    srv — слушатель, принявший hello (по умолчанию — слушатель процесса)."""
    if not _fleet(services):
        return
    async with _LOCK:
        try:
            out = await asyncio.to_thread(services.fleet_hello, slot_id)
            await _run(services, out, srv)
        except Exception as e:                            # noqa: BLE001
            log.warning("набор: hello слота %s не разобран: %s", slot_id, e)


async def on_ack(services, slot_id: int, msg: dict, srv=None) -> None:
    """`upd_ack` шлюза."""
    if not _fleet(services):
        return
    async with _LOCK:
        out = await asyncio.to_thread(services.fleet_ack, slot_id, msg)
        await _run(services, out, srv)


async def on_start(services) -> None:
    """Старт бота: откат сервера сторожем (Т-У8), затем такт — ход и откат
    продолжаются с записанного."""
    if not _fleet(services):
        return
    async with _LOCK:
        try:
            out = await asyncio.to_thread(services.fleet_on_start)
            await _run(services, out)
        except Exception as e:                            # noqa: BLE001
            log.warning("набор: старт: %s", e)
    await fleet_run_tick(services)
