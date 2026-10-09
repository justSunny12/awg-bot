"""
handlers/admin/blocks.py — ручные блокировки (админ): устройство и профиль.

Профиль: «Приостановить подписку на время блокировки?» [⏸️ Да] [▶️ Нет] →
«Уведомить владельца профиля?» [🔔 Да] [🔕 Нет]; пауза — всегда до снятия
блокировки, выбора срока нет. Устройство: [🔔 С уведомлением] [🔕 Тихо].
"""

from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.types import CallbackQuery

from awgbot.bot.callbacks import BlockCB
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.notifier import send_notifications
from awgbot.bot.handlers.admin.clients import _show_client_card
from awgbot.bot.handlers.admin.devices import device_card_parts
from awgbot.bot.handlers.admin.panel import _bot

router = Router(name="admin.blocks")

from awgbot.core.blocks import DeviceBlock as DeviceBlock, ClientBlock as ClientBlock

_KIND_TO_DEV = {"silent": DeviceBlock.ADMIN_SILENT, "notified": DeviceBlock.ADMIN_NOTIFIED,
                "user": DeviceBlock.USER}
_KIND_TO_CLI = {"silent": ClientBlock.ADMIN_SILENT, "notified": ClientBlock.ADMIN_NOTIFIED,
                "user": ClientBlock.USER}


async def _rerender_after_block(cb, services, target: str, ref: int):
    if target == "cli":
        await _show_client_card(cb, services, ref, answer=False)   # всплывашку даёт вызывающий
    else:
        dev = await call(services.db.get_device, ref)
        if dev:
            await edit(cb, *await device_card_parts(services, dev))


@router.callback_query(BlockCB.filter(F.action == "menu_block"))
async def admin_block_menu(cb: CallbackQuery, callback_data: BlockCB, services):
    """Профиль — сперва про паузу подписки; устройство — сразу как."""
    if callback_data.target == "cli":
        client = await call(services.db.get_client, callback_data.ref)
        if client is None:
            await cb.answer(ui.Toast.no_profile, show_alert=True)
            return
        await edit(cb, texts.block_client_ask(client, _bot(services)), kb.block_pause_kb(client.id))
    else:
        dev = await call(services.db.get_device, callback_data.ref)
        if dev is None:
            await cb.answer(ui.Toast.no_device, show_alert=True)
            return
        owner = await call(services.db.get_client, dev.client_id)
        await edit(cb, texts.block_device_ask_admin(dev.name, None if owner is None or owner.is_service else owner,
                                                   _bot(services)),
                   kb.block_notify_kb("dev", dev.id))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "pause_no"))
async def admin_block_pause_no(cb: CallbackQuery, callback_data: BlockCB):
    await edit(cb, texts.BLOCK_NOTIFY_ASK, kb.block_notify_kb("cli", callback_data.ref, pause_days=-1))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "pause_yes"))
async def admin_block_pause_yes(cb: CallbackQuery, callback_data: BlockCB):
    """Пауза — до снятия блокировки (0 = бессрочно)."""
    await edit(cb, texts.BLOCK_NOTIFY_ASK, kb.block_notify_kb("cli", callback_data.ref, pause_days=0))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "menu_unblock"))
async def admin_unblock_menu(cb: CallbackQuery, callback_data: BlockCB, services):
    """Одна ручная причина — снять сразу; несколько — выбор."""
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
        await cb.answer(ui.Toast.unblocked)
        return
    await edit(cb, "Какую блокировку снять?", kb.block_unblock_reasons(target, ref, mask))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "block"))
async def admin_block_do(cb: CallbackQuery, callback_data: BlockCB, services):
    notify = callback_data.kind == "notified"
    pd = callback_data.days
    pause_days = None if pd < 0 else pd         # -1 = без приостановки, 0 = до снятия
    if callback_data.target == "cli":
        bit = _KIND_TO_CLI["notified" if notify else "silent"]
        notes = await call(services.block_client_manual, callback_data.ref, bit, notify, pause_days)
        obj = await call(services.db.get_client, callback_data.ref)
    else:
        bit = _KIND_TO_DEV["notified" if notify else "silent"]
        notes = await call(services.block_device_manual, callback_data.ref, bit, notify,
                           actor_tg=cb.from_user.id)
        obj = await call(services.db.get_device, callback_data.ref)
    owner = ""
    if callback_data.target != "cli" and obj is not None:
        oc = await call(services.db.get_client, obj.client_id)
        owner = oc.name if oc is not None and not oc.is_service else ""
    await send_notifications(cb.bot, notes)
    await _rerender_after_block(cb, services, callback_data.target, callback_data.ref)
    await cb.answer(texts.blocked_toast(obj.name if obj else "", silent=not notify,
                                        profile=callback_data.target == "cli", owner=owner))


async def _do_unblock(cb, services, target: str, ref: int, kind: str):
    kinds = ["silent", "notified", "user"] if kind == "all" else [kind]
    notes = []
    if target == "cli":
        obj = await call(services.db.get_client, ref)
        mask = int(obj.block_reason) if obj else 0
        for k in kinds:
            bit = _KIND_TO_CLI.get(k)
            if bit is None or not (mask & int(bit)):
                continue
            notes += await call(services.unblock_client_manual, ref, bit, k != "silent")
    else:
        obj = await call(services.db.get_device, ref)
        mask = int(obj.block_reason) if obj else 0
        for k in kinds:
            bit = _KIND_TO_DEV.get(k)
            if bit is None or not (mask & int(bit)):
                continue
            notes += await call(services.unblock_device_manual, ref, bit, k != "silent",
                                actor_tg=cb.from_user.id)
    await send_notifications(cb.bot, notes)
    await _rerender_after_block(cb, services, target, ref)


@router.callback_query(BlockCB.filter(F.action == "unblock"))
async def admin_unblock_do(cb: CallbackQuery, callback_data: BlockCB, services):
    await _do_unblock(cb, services, callback_data.target, callback_data.ref, callback_data.kind)
    await cb.answer(ui.Toast.unblocked)


@router.callback_query(BlockCB.filter(F.action == "cancel"))
async def admin_block_cancel(cb: CallbackQuery, callback_data: BlockCB, services):
    await _rerender_after_block(cb, services, callback_data.target, callback_data.ref)
    await cb.answer()
