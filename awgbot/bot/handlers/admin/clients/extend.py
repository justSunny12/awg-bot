"""extend.py — часть пакета clients (см. __init__): продление с тумблером остатка, снятие паузы администратором."""
from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.bot.callbacks import ClientCB, Menu, PeriodCB
from awgbot.bot.handlers.common import call, edit, send_menu
from awgbot.bot.notifier import send_notifications
from awgbot.domain.services import ServiceError
from awgbot.bot.handlers.admin.panel import expiring_screen, _panel_parts, _bot
from .card import router, client_card_parts, _show_client_card
from .edit import _client_or_alert


# ─────────────────────────────────────────────────────────────────────────────
# Продление: срок сразу, «сохранить остаток» тумблером
# ─────────────────────────────────────────────────────────────────────────────

async def extend_screen(services, client_id: int, *, keep: bool = True, cancel_to=None):
    """Экран продления или None — профиля нет."""
    client = await call(services.db.get_client, client_id)
    if client is None:
        return None
    from awgbot.domain.services import SECONDS_PER_DAY
    cut_days = int(client.grace_pending_cut) // SECONDS_PER_DAY
    remainder = await call(services.remaining_for, client_id)
    return (texts.extend_text(client, cut_days, _bot(services)),
            kb.period_kb("extend", client_id, min_days=cut_days, keep=keep,
                         has_remainder=remainder > 0, cancel_cb=cancel_to))


@router.callback_query(ClientCB.filter(F.action.in_(("extend", "extend_exp"))))
async def extend_start(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    """extend_exp — из списка истекающих: после продления или отмены —
    обратно в список, пока он не пуст."""
    if callback_data.action == "extend_exp":
        await state.update_data(return_to="expiring")
    return_to = (await state.get_data()).get("return_to")
    screen = await extend_screen(
        services, callback_data.client_id,
        cancel_to=Menu(action="expiring").pack() if return_to == "expiring" else None)
    if screen is None:
        await cb.answer(ui.Toast.no_profile, show_alert=True)
        return
    await edit(cb, *screen)
    await cb.answer()


@router.callback_query(PeriodCB.filter((F.ctx == "extend") & (F.kind == "keep_tgl")))
async def extend_keep_toggle(cb: CallbackQuery, callback_data: PeriodCB, services, state: FSMContext):
    return_to = (await state.get_data()).get("return_to")
    screen = await extend_screen(
        services, callback_data.ref, keep=bool(callback_data.keep),
        cancel_to=Menu(action="expiring").pack() if return_to == "expiring" else None)
    if screen is None:
        await cb.answer(ui.Toast.no_profile, show_alert=True)
        return
    await edit(cb, *screen)
    await cb.answer()


@router.callback_query(PeriodCB.filter(F.ctx == "extend"))
async def extend_period_chosen(cb: CallbackQuery, callback_data: PeriodCB, services, state: FSMContext):
    """Срок выбран — продление сразу; остаток — по тумблеру в колбэке."""
    return_to = (await state.get_data()).get("return_to")
    await state.clear()
    keep = bool(callback_data.keep) and callback_data.kind != "never"
    await _do_extend(cb, services, callback_data.ref, callback_data.kind, keep=keep, return_to=return_to)


async def _do_extend(cb, services, client_id, kind, keep: bool, return_to: str | None = None):
    """Итог — след в чате двумя строками, следом — откуда пришли: список
    истекающих (пока не пуст) или карточка профиля."""
    try:
        result = await call(services.extend_period, client_id, kind, keep)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    await send_notifications(cb.bot, result.notifications)
    fresh = await call(services.db.get_client, client_id)
    await cb.answer(ui.Toast.extended)
    await edit(cb, texts.extended_note(fresh, kind, result.new_end, result.pause, _bot(services)), None)
    keep_id = cb.message.message_id
    if return_to == "expiring" and await call(services.expiring_subscriptions):
        await send_menu(cb.message, services, *await expiring_screen(services), keep_id=keep_id)
        return
    parts = await client_card_parts(services, client_id)
    if parts is None:
        await send_menu(cb.message, services, *await _panel_parts(services), keep_id=keep_id)
        return
    await send_menu(cb.message, services, *parts, keep_id=keep_id)


# ── админ снимает паузу клиента ──────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "resume_pause"))
async def admin_resume_pause(cb: CallbackQuery, callback_data: ClientCB, services):
    """Запасной выход из клиентской паузы (клиент заперся: Telegram только через
    этот VPN). Тот же exit_pause, что у клиента."""
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    ok, actual, new_end, notes = await call(services.exit_pause, client.id, auto=False)
    if not ok:
        await cb.answer("Профиль не на паузе", show_alert=True)
        await _show_client_card(cb, services, client.id, answer=False)
        return
    await send_notifications(cb.bot, notes)
    await cb.answer(ui.Toast.pause_lifted)
    await edit(cb, texts.resumed_note(client, actual, new_end, _bot(services)), None)
    parts = await client_card_parts(services, client.id)
    if parts is not None:
        await send_menu(cb.message, services, *parts, keep_id=cb.message.message_id)
