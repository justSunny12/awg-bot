"""pause.py — часть пакета client (см. __init__): помощь и отсрочка, ручная блокировка своего устройства, пауза подписки."""
from __future__ import annotations

import datetime
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot import keyboards as kb
from awgbot.bot import screens
from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.bot.callbacks import BlockCB, GraceCB, HelpCB, PauseCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.bot.handlers.common import (
    call, cleanup_content, edit, ask_here, ask_tracked, back_to_context, mine_or_held)
from awgbot.bot.states import PauseDays
from awgbot.core.enums import PauseMode, PeriodKind
from awgbot.bot.handlers import devcore
from .main import router, _bot_username, _show_main
from .menu import sub_parts, device_card_parts


# ── помощь (гайды — в handlers/guide.py) ─────────────────────────────────────

@router.callback_query(HelpCB.filter(F.platform == "root"))
async def help_root(cb: CallbackQuery):
    await edit(cb, texts.HELP_INTRO, kb.help_menu())
    await cb.answer()


@router.callback_query(HelpCB.filter(F.platform == "skip"))
async def help_skip(cb: CallbackQuery, client, services):
    await _show_main(None, services, client, via_edit=cb)
    await cb.answer()


@router.callback_query(GraceCB.filter(F.action == "take"))
async def grace_take(cb: CallbackQuery, callback_data: GraceCB, client, services):
    """Клиент активирует отсрочку. Защита от протухшей кнопки — внутри
    activate_grace (истёк/использовано/не годовой → неактуально)."""
    if callback_data.ref != client.id:
        await cb.answer(texts.GRACE_STALE, show_alert=True)
        return
    grace_days = settings.get_int("grace.grace_days", 14)
    ok, new_end = await call(services.activate_grace, client.id, grace_days)
    if not ok:
        await cb.answer(texts.GRACE_STALE, show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                              # noqa: BLE001
            pass
        return
    await edit(cb, texts.grace_activated_client(grace_days, timeutil.fmt_date_ui(new_end)), None)
    if settings.get_bool("notifications.client_events.grace", True):
        await notify_one(cb.message.bot, config.ADMIN_ID,
                         texts.grace_activated_admin(texts.profile_link(client, _bot_username(services)), grace_days))
    await cb.answer(texts.grace_taken_toast(grace_days))


# ── ручная блокировка своего устройства ──────────────────────────────────────



async def _blockable(services, client, callback_data: BlockCB):
    """Своё (не переданное — там управляет держатель) или удерживаемое чужое."""
    if callback_data.target != "dev":
        return None
    dev = await call(mine_or_held, services, client, callback_data.ref)
    if dev is None or (dev.is_lent and dev.holder_client_id != client.id):
        return None
    return dev


@router.callback_query(BlockCB.filter(F.action == "menu_block"))
async def client_block_ask(cb: CallbackQuery, callback_data: BlockCB, client, services):
    """Блокировка — с подтверждением: может отрезать от бота."""
    dev = await _blockable(services, client, callback_data)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    held = dev.holder_client_id == client.id          # чужое в держании: владелец узнает
    await edit(cb, texts.block_device_ask(dev.name, owner=texts.owner_link(dev) if held else ""),
               kb.block_device_confirm(dev.id))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "block"))
async def client_block_device(cb: CallbackQuery, callback_data: BlockCB, client, services):
    dev = await _blockable(services, client, callback_data)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await devcore.block_device(cb, services, dev, lambda d: device_card_parts(services, client, d))


@router.callback_query(BlockCB.filter(F.action == "menu_unblock"))
async def client_unblock_device(cb: CallbackQuery, callback_data: BlockCB, client, services):
    """Снимает ТОЛЬКО свой USER-бит; админские биты остаются."""
    dev = await _blockable(services, client, callback_data)
    if dev is None:
        await cb.answer(ui.Toast.no_device, show_alert=True)
        return
    await devcore.unblock_device(cb, services, dev, lambda d: device_card_parts(services, client, d))


# ── пауза подписки ───────────────────────────────────────────────────────────

async def _show_sub(cb, client, services):
    parts = await sub_parts(services, client.id)
    if parts is not None:
        await edit(cb, *parts)


@router.callback_query(PauseCB.filter(F.action == "ask"))
async def pause_ask(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    await state.clear()
    avail = await call(services.pause_available_days, client.id)
    if avail <= 0:
        if client.period_kind == PeriodKind.YEAR:
            await cb.answer(texts.pause_limit_exhausted(), show_alert=True)
        else:
            await cb.answer(texts.pause_unavailable(), show_alert=True)
        return
    email = await call(services.email_resume_enabled)
    await edit(cb, texts.pause_ask(avail, email_resume=bool(email)), kb.pause_kb(client.id, avail))
    await cb.answer()


async def _enter_pause(cb_or_msg, services, client, days: int, *, via_cb: CallbackQuery | None) -> None:
    """Поставить паузу и показать итог первой строкой экрана «💳 Подписка»
    (там же «▶️ Снять паузу»); аварийный код (если почтовый выход включён) —
    отдельным сообщением: его надо сохранить."""
    ok, reserved, notes, code = await call(services.enter_pause, client.id, days or None)
    if not ok:
        if via_cb is not None:
            await via_cb.answer(texts.pause_unavailable(), show_alert=True)
            await _show_sub(via_cb, client, services)
        else:
            await back_to_context(cb_or_msg, services, {"ctx_kind": "sub"}, "client", client,
                                  note="⚠️ " + texts.pause_unavailable())
        return
    await send_notifications(cb_or_msg.bot, notes)     # друзьям — о постановке
    fresh = await call(services.db.get_client, client.id)
    until = timeutil.fmt_end_ui(
        timeutil.parse_iso(fresh.pause_active_since)
        + datetime.timedelta(days=int(fresh.pause_reserved_days)))
    summary = texts.pause_entered_summary(until)
    message = via_cb.message if via_cb is not None else cb_or_msg
    if via_cb is not None:
        await via_cb.answer(f"Пауза: {reserved} дн.")
        await cleanup_content(message.bot, services, message.chat.id, keep=message.message_id)
        parts = await sub_parts(services, client.id)
        if parts is not None:
            await edit(via_cb, screens.with_note(parts[0], summary), parts[1])
    else:
        await back_to_context(message, services, {"ctx_kind": "sub"}, "client", client, note=summary)
    if code and await call(services.email_resume_enabled):
        await message.answer(texts.pause_emergency_code(code, await call(services.email_resume_address)))


@router.callback_query(PauseCB.filter(F.action == "pick"))
async def pause_pick(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    """Выбор дней = подтверждение: пауза ставится сразу."""
    await state.clear()
    avail = await call(services.pause_available_days, client.id)
    days = max(1, min(int(callback_data.days), avail))
    await _enter_pause(cb, services, client, days, via_cb=cb)


@router.callback_query(PauseCB.filter(F.action == "other"))
async def pause_other(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    avail = await call(services.pause_available_days, client.id)
    await state.set_state(PauseDays.value)
    await ask_here(cb, services, state, texts.pause_other_prompt(avail), "sub", client_id=client.id)
    await cb.answer()


@router.message(PauseDays.value, RoleFilter("client"))
async def pause_other_apply(message: Message, client, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    avail = await call(services.pause_available_days, client.id)
    if not raw.isdigit() or not (1 <= int(raw) <= avail):
        await ask_tracked(message, services, texts.pause_days_bad(avail))
        return
    await state.clear()
    await _enter_pause(message, services, client, int(raw), via_cb=None)


async def _user_pause_guard(cb, client, services) -> bool:
    """True — у клиента активна ЕГО СОБСТВЕННАЯ пауза (mode=user)."""
    fresh = await call(services.db.get_client, client.id)
    if fresh is None or not fresh.is_paused:
        await cb.answer(ui.Toast.not_paused, show_alert=True)
        await _show_sub(cb, client, services)
        return False
    if fresh.pause_mode != PauseMode.USER:
        await cb.answer("Эту паузу поставил администратор — снять её может только он",
                        show_alert=True)
        await _show_sub(cb, client, services)
        return False
    return True


@router.callback_query(PauseCB.filter(F.action == "resume"))
async def pause_resume(cb: CallbackQuery, callback_data: PauseCB, client, services):
    """Снять паузу — сразу: сколько спишется, видно на экране подписки."""
    if not await _user_pause_guard(cb, client, services):
        return
    ok, actual, new_end, notes = await call(services.exit_pause, client.id, auto=False)
    if not ok:
        await cb.answer(ui.Toast.not_paused, show_alert=True)
        await _show_sub(cb, client, services)
        return
    await send_notifications(cb.bot, notes)     # друзьям — о снятии
    await cb.answer(ui.Toast.pause_lifted)
    parts = await sub_parts(services, client.id)   # итог — первой строкой того же экрана
    if parts is not None:
        await edit(cb, screens.with_note(parts[0], texts.pause_resumed_self(actual, new_end)), parts[1])


@router.callback_query(PauseCB.filter(F.action == "cancel"))
async def pause_cancel(cb: CallbackQuery, callback_data: PauseCB, client, services, state: FSMContext):
    await state.clear()
    await _show_sub(cb, client, services)
    await cb.answer()
