"""
handlers/admin/blocks.py — ручные блокировки (админ): устройство и клиент.
"""

from __future__ import annotations

from awgbot.bot import keyboards as kb
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import BlockCB
from awgbot.bot.handlers.common import call, edit, ask_tracked, park_screen, send_menu
from awgbot.bot.notifier import send_notifications
from awgbot.bot.states import BlockPauseDays
from awgbot.bot.handlers.admin.clients import _show_client_card
from awgbot.bot.handlers.admin.devices import _device_card_parts

router = Router(name="admin.blocks")


# ─────────────────────────────────────────────────────────────────────────────
# Ручные блокировки (админ): устройство и клиент
# ─────────────────────────────────────────────────────────────────────────────

from awgbot.core.blocks import DeviceBlock as DeviceBlock, ClientBlock as ClientBlock

_KIND_TO_DEV = {"silent": DeviceBlock.ADMIN_SILENT, "notified": DeviceBlock.ADMIN_NOTIFIED,
                "user": DeviceBlock.USER}
_KIND_TO_CLI = {"silent": ClientBlock.ADMIN_SILENT, "notified": ClientBlock.ADMIN_NOTIFIED,
                "user": ClientBlock.USER}


async def _rerender_after_block(cb, services, target: str, ref: int):
    """Перерисовать карточку устройства/клиента после изменения блокировки."""
    if target == "cli":
        await _show_client_card(cb, services, ref)
    else:
        dev = await call(services.db.get_device, ref)
        if dev:
            await edit(cb, *await _device_card_parts(services, dev))


@router.callback_query(BlockCB.filter(F.action == "menu_block"))
async def admin_block_menu(cb: CallbackQuery, callback_data: BlockCB):
    """Админ жмёт «Заблокировать». Для КЛИЕНТА сперва спрашиваем про приостановку
    подписки; для устройства — сразу выбор уведомления (пауза только для клиента)."""
    if callback_data.target == "cli":
        await edit(cb, "Приостановить подписку на время блокировки?",
                   kb.block_pause_choice(callback_data.ref))
    else:
        await edit(cb, "Как заблокировать устройство?",
                   kb.block_notify_choice("dev", callback_data.ref))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "pause_no"))
async def admin_block_pause_no(cb: CallbackQuery, callback_data: BlockCB):
    """Блок клиента без приостановки → выбор уведомления (pause_days=-1)."""
    await edit(cb, "Как заблокировать профиль?",
               kb.block_notify_choice("cli", callback_data.ref, pause_days=-1))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "pause_yes"))
async def admin_block_pause_yes(cb: CallbackQuery, callback_data: BlockCB, state: FSMContext,
                                services):
    """Блок клиента с приостановкой → ввод длительности (0 = бессрочно)."""
    await state.set_state(BlockPauseDays.days)
    await state.update_data(block_client=callback_data.ref)
    await park_screen(cb, services)
    await ask_tracked(cb.message, services,
                      "На сколько дней приостановить подписку? Введи число (0 — бессрочно, "
                      "до снятия блокировки).", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(BlockPauseDays.days)
async def admin_block_pause_days(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, "Введи целое число дней (0 — бессрочно):")
        return
    data = await state.get_data()
    client_id = data.get("block_client")
    await state.clear()
    days = int(raw)
    _acc = await message.answer("Принято.", reply_markup=kb.reply_hide())
    await call(services.db.add_content_msg_id, _acc.chat.id, _acc.message_id)
    # экран с кнопками — через send_menu: он живой, прежний гаснет
    await send_menu(message, services,
                    f"Приостановка: {'бессрочно' if days == 0 else f'{days} дн.'}. "
                    "Как заблокировать профиль?",
                    kb.block_notify_choice("cli", client_id, pause_days=days))


@router.callback_query(BlockCB.filter(F.action == "menu_unblock"))
async def admin_unblock_menu(cb: CallbackQuery, callback_data: BlockCB, services):
    """Админ жмёт «Разблокировать». Если активна ровно одна ручная причина —
    снимаем сразу, без диалога (он избыточен — выбирать не из чего). Если
    несколько — спрашиваем, какую именно."""
    target, ref = callback_data.target, callback_data.ref
    if target == "cli":
        obj = await call(services.db.get_client, ref)
        kind_map = _KIND_TO_CLI
    else:
        obj = await call(services.db.get_device, ref)
        kind_map = _KIND_TO_DEV
    if obj is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    mask = int(obj.block_reason)
    active_kinds = [k for k, bit in kind_map.items() if mask & int(bit)]
    if len(active_kinds) <= 1:
        kind = active_kinds[0] if active_kinds else "all"
        await _do_unblock(cb, services, target, ref, kind)
        await cb.answer("Разблокировано")
        return
    await edit(cb, "Какую блокировку снять?",
               kb.block_unblock_reasons(target, ref, mask))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "block"))
async def admin_block_do(cb: CallbackQuery, callback_data: BlockCB, services):
    notify = callback_data.kind == "notified"
    pd = callback_data.days
    pause_days = None if pd < 0 else pd         # -1 = без приостановки
    if callback_data.target == "cli":
        bit = _KIND_TO_CLI["notified" if notify else "silent"]
        notes = await call(services.block_client_manual, callback_data.ref, bit,
                           notify, pause_days)
    else:
        bit = _KIND_TO_DEV["notified" if notify else "silent"]
        notes = await call(services.block_device_manual, callback_data.ref, bit, notify)
    await send_notifications(cb.bot, notes)
    await _rerender_after_block(cb, services, callback_data.target, callback_data.ref)
    if callback_data.target == "cli":
        _o = await call(services.db.get_client, callback_data.ref)
        _what = f"🛑 Профиль «{_o.name}» заблокирован" if _o else "🛑 Профиль заблокирован"
    else:
        _o = await call(services.db.get_device, callback_data.ref)
        _what = f"🛑 Устройство «{_o.name}» заблокировано" if _o else "🛑 Устройство заблокировано"
    await cb.answer(_what + ("" if notify else " (тихо)"))


async def _do_unblock(cb, services, target: str, ref: int, kind: str):
    """Снимает блокировку (kind или 'all') + уведомления + перерисовка карточки.
    Общий путь и для диалога выбора, и для авто-снятия единственной причины."""
    if target == "cli":
        obj = await call(services.db.get_client, ref)
        mask = int(obj.block_reason) if obj else 0
        kinds = (["silent", "notified", "user"] if kind == "all" else [kind])
        notes = []
        for k in kinds:
            bit = _KIND_TO_CLI.get(k)
            if bit is None or not (mask & int(bit)):
                continue
            notes += await call(services.unblock_client_manual, ref, bit, k != "silent")
    else:
        obj = await call(services.db.get_device, ref)
        mask = int(obj.block_reason) if obj else 0
        kinds = (["silent", "notified", "user"] if kind == "all" else [kind])
        notes = []
        for k in kinds:
            bit = _KIND_TO_DEV.get(k)
            if bit is None or not (mask & int(bit)):
                continue
            notes += await call(services.unblock_device_manual, ref, bit, k != "silent")
    await send_notifications(cb.bot, notes)
    await _rerender_after_block(cb, services, target, ref)


@router.callback_query(BlockCB.filter(F.action == "unblock"))
async def admin_unblock_do(cb: CallbackQuery, callback_data: BlockCB, services):
    await _do_unblock(cb, services, callback_data.target, callback_data.ref, callback_data.kind)
    await cb.answer("Разблокировано")


@router.callback_query(BlockCB.filter(F.action == "cancel"))
async def admin_block_cancel(cb: CallbackQuery, callback_data: BlockCB, services):
    await _rerender_after_block(cb, services, callback_data.target, callback_data.ref)
    await cb.answer()
