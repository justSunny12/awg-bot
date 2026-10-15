"""delete.py — часть пакета clients (см. __init__): новое приглашение и удаление профиля."""
from __future__ import annotations

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from aiogram import F
from aiogram.types import CallbackQuery
from awgbot.bot.callbacks import ClientCB
from awgbot.bot.handlers.common import call, edit, send_menu, drop_message
from awgbot.bot.notifier import notify_one
from awgbot.domain.services import ServiceError
from .card import router, clients_screen
from .create import _invite_menu
from .edit import _client_or_alert


# ─────────────────────────────────────────────────────────────────────────────
# Новое приглашение / удаление
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(ClientCB.filter(F.action == "regen_invite"))
async def regen_invite(cb: CallbackQuery, callback_data: ClientCB, services):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    try:
        code = await call(services.regenerate_invite, client.id)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    await cb.answer("Приглашение перевыпущено")
    await drop_message(cb, services)
    await _invite_menu(cb.message, services, client, code, new=False)


@router.callback_query(ClientCB.filter(F.action == "delete"))
async def client_delete_confirm(cb: CallbackQuery, callback_data: ClientCB, services):
    client = await _client_or_alert(cb, services, callback_data.client_id)
    if client is None:
        return
    devices = await call(services.db.list_devices, client.id)
    lent = [d for d in devices if getattr(d, "is_lent", False)]
    await edit(cb, texts.client_delete_ask(client, devices, lent), kb.client_delete_confirm(client.id))
    await cb.answer()


@router.callback_query(ClientCB.filter(F.action == "delete_yes"))
async def client_delete_apply(cb: CallbackQuery, callback_data: ClientCB, services):
    target = await _client_or_alert(cb, services, callback_data.client_id)
    if target is None:
        return
    if target.tg_id == config.ADMIN_ID:
        await cb.answer("Профиль администратора нельзя удалить", show_alert=True)
        return
    res = await call(services.delete_client_with_devices, target.id)
    # снятые устройства сняты в любом случае: их держатели узнают сразу, иначе
    # при частичном отказе VPN у них просто гаснет без объяснений
    for holder_tg, d in res["removed"]:
        if holder_tg:
            await notify_one(cb.bot, holder_tg, texts.lent_device_deleted_by_admin_notice(d))
    if res["failed"]:
        await edit(cb, texts.client_delete_partial(target.name, res["failed"]), kb.client_delete_retry(target.id))
        await cb.answer(texts.CLIENT_DELETE_PARTIAL_TOAST, show_alert=True)
        return
    await cb.answer()
    await edit(cb, texts.client_deleted_note(target.name, res["n"]), None)
    await send_menu(cb.message, services, *await clients_screen(services, cb.message.chat.id),
                    keep_id=cb.message.message_id)
