"""
handlers/fleet.py — набор обновлений в чате бота сервера AWG: диалог
«⬆️ Обновить до vX?», старт хода, живое сообщение хода и его итог,
«🔁 Повторить», «⬅️ Отмена».

Ход ведёт домен (domain/fleet) по тактам канала (runtime/linkserver/upd.py);
здесь — только человек: кнопки и одно живое сообщение без звука, которое
правится по ходу и закрывается итогом (готово, набор откатился, откат не прошёл) с кнопками.
"""
from __future__ import annotations

import logging

from aiogram.types import CallbackQuery

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.handlers.common import call, cleanup_content, dismiss_update_reports, drop_message, edit_nav
from awgbot.bot.texts import fleet as ft

log = logging.getLogger("awgbot.fleet")

_REFUSALS = {"running": ft.UPDATE_RUNNING_TOAST, "nothing": texts.UPDATE_NOTHING,
             "changed": texts.UPDATE_TARGET_CHANGED_TOAST}


def _user(services) -> str:
    return getattr(services, "bot_username", "") or ""


async def _is_live_menu(cb: CallbackQuery, services) -> bool:
    try:
        live = await call(services.db.get_nav_message_id, cb.message.chat.id)
    except Exception:                                     # noqa: BLE001
        return False
    return live is not None and live == cb.message.message_id


async def fleet_show(bot, services, out=None) -> None:
    """Живое сообщение хода — по текущему ходу; итог — с кнопками; затем
    уведомления такта (догон, откат сервера)."""
    run = await call(services.fleet_run)
    msg = run.get("msg") or []
    # итог прошлого хода не перерисовывается тактами вне хода (догон,
    # следование): только идущий ход и его последний такт
    active = run.get("state") in ("running", "rolling_back")
    if len(msg) == 2 and (out is None or (out.live and (active or out.final))):
        plan = await call(services.fleet_run_plan)
        state = run.get("state") or ""
        final = state not in ("running", "rolling_back")
        markup = kb.fleet_final(retry=state in ("rolled_back", "rollback_failed")) if final else None
        try:
            await bot.edit_message_text(ft.fleet_live(plan, bot_username=_user(services)), chat_id=msg[0],
                                        message_id=msg[1], reply_markup=markup)
        except Exception as e:                            # noqa: BLE001 — не изменилось или удалено
            if "not modified" not in str(e):
                log.info("набор: живое сообщение не правится: %s", e)
        if final:
            await call(services.remember_update_report, msg[0], msg[1])
    if out is not None and out.notes:
        from awgbot.bot.notifier import send_notifications
        await send_notifications(bot, list(out.notes))


async def install(cb: CallbackQuery, services, tag: str = "", rerender=None) -> None:
    """«⬆️ Обновить до vX» (раздел, уведомление) — диалог с порядком обновления.
    Цель по сохранённому списку, без сети; сменилась — всплывашка."""
    if await call(services.fleet_running):
        await cb.answer(ft.UPDATE_RUNNING_TOAST, show_alert=True)
        return
    target = await call(services.fleet_target)
    if target is None or not tag or target.tag != tag:
        await cb.answer(texts.UPDATE_TARGET_CHANGED_TOAST if target is not None else texts.UPDATE_NOTHING,
                        show_alert=True)
        if rerender is not None and await _is_live_menu(cb, services):
            await rerender()
        return
    blocked = await call(services.update_block_reason, target)
    if blocked:
        await cb.answer(texts.update_blocked_toast(blocked), show_alert=True)
        return
    plan = await call(services.fleet_plan, target)
    text, markup = ft.fleet_confirm(plan), kb.fleet_confirm(target.tag)
    if await _is_live_menu(cb, services):
        await edit_nav(cb, services, text, markup)
    else:
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception:                                 # noqa: BLE001
            pass
    await cb.answer()


async def cancel(cb: CallbackQuery, services, rerender=None) -> None:
    """«⬅️ Отмена» диалога: из живого меню — раздел; уведомление — прочь."""
    await cb.answer()
    if rerender is not None and await _is_live_menu(cb, services):
        await rerender()
        return
    await drop_message(cb, services)


async def start(cb: CallbackQuery, services, tag: str) -> None:
    """«⬆️ Обновить»: сначала свежий список с GitHub (не ответил — хода нет),
    затем ход; диалог и контент — прочь, живое сообщение — одно, без звука."""
    nxt, failed = await call(services.update_check)
    if failed:
        await cb.answer(texts.UPDATE_CHECK_FAILED_TOAST, show_alert=True)
        return
    why = await call(services.fleet_start, tag)
    if why:
        await cb.answer(_REFUSALS.get(why, texts.UPDATE_TARGET_CHANGED_TOAST), show_alert=True)
        return
    await _begin(cb, services, drop=True)


async def retry(cb: CallbackQuery, services) -> None:
    """«🔁 Повторить»: набор заново с того, что стоит; догоны — заново."""
    why = await call(services.fleet_retry)
    if why:
        await cb.answer(_REFUSALS.get(why, texts.UPDATE_TARGET_CHANGED_TOAST), show_alert=True)
        return
    if not await call(services.fleet_running):
        await cb.answer(texts.UPDATE_STARTING)
        from awgbot.runtime.linkserver import upd as fleet_upd
        await fleet_upd.fleet_run_tick(services)
        return
    live = await _is_live_menu(cb, services)
    if not live:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)   # итог прошлого хода остаётся в истории
        except Exception:                                 # noqa: BLE001
            pass
    await _begin(cb, services, drop=live)


async def _begin(cb: CallbackQuery, services, *, drop: bool) -> None:
    """Живое сообщение нового хода и первый такт; drop — убрать сообщение
    с кнопкой (диалог или раздел)."""
    await cb.answer(texts.UPDATE_STARTING)
    chat_id = cb.message.chat.id
    await dismiss_update_reports(cb.bot, services)
    await cleanup_content(cb.bot, services, chat_id)
    if drop:
        await drop_message(cb, services)
    plan = await call(services.fleet_run_plan)
    sent = await cb.bot.send_message(chat_id, ft.fleet_live(plan, bot_username=_user(services)),
                                     disable_notification=True)
    await call(services.fleet_set_message, chat_id, sent.message_id)
    from awgbot.runtime.linkserver import upd as fleet_upd
    await fleet_upd.fleet_run_tick(services)
