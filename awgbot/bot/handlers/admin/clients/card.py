"""card.py — часть пакета clients (см. __init__): роутер, список, карточка, подменю «✏️ Изменить»."""
from __future__ import annotations

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.bot.callbacks import ClientCB, Menu
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.handlers.admin.panel import _bot


router = Router(name="admin.clients")


# ─────────────────────────────────────────────────────────────────────────────
# Список / карточка / «✏️ Изменить»
# ─────────────────────────────────────────────────────────────────────────────

async def clients_screen(services, chat_id: int = 0):
    clients = await call(services.db.list_clients, exclude_tg=config.ADMIN_ID)
    online = await call(services.online_client_ids)
    from awgbot.bot import paging
    n_online = sum(1 for c in clients if c.id in online)
    return (texts.profiles_header(len(clients), n_online),
            kb.admin_clients(clients, online, page=paging.page_of(chat_id, "clients")))


@router.callback_query(Menu.filter(F.action == "clients"))
async def clients_list(cb: CallbackQuery, services, state: FSMContext):
    # Профиль админа скрыт: его устройства и РФ-доступ — на главной.
    await state.clear()
    await cb.answer()
    await edit(cb, *await clients_screen(services, cb.message.chat.id))


async def client_card_parts(services, client_id: int):
    """(текст, клавиатура) карточки профиля или None — профиля нет."""
    d = await call(services.client_card_data, client_id)
    if d is None or d["client"].tg_id == config.ADMIN_ID:   # у профиля админа карточки нет
        return None
    client = d["client"]
    if d.get("rt_visible"):
        d["rt_counts"] = await call(services.routing_device_counts, client_id)
    return (texts.admin_client_card(d, _bot(services)),
            kb.admin_client_actions(client, d["devices"], routing_visible=d["rt_visible"]))


async def client_edit_parts(services, client_id: int):
    client = await call(services.db.get_client, client_id)
    if client is None:
        return None
    return texts.client_edit_text(client), kb.client_edit_kb(client_id)


async def _show_client_card(cb: CallbackQuery, services, client_id: int, *, answer: bool = True):
    parts = await client_card_parts(services, client_id)
    if parts is None:
        client = await call(services.db.get_client, client_id)
        if client is not None and client.tg_id == config.ADMIN_ID:
            from awgbot.bot.handlers.admin import panel      # профиль админа — главная
            await edit(cb, *await panel._panel_parts(services))
            if answer:
                await cb.answer()
            return
        await cb.answer(ui.Toast.no_profile, show_alert=True)   # единственный ответ: иначе alert теряется
        return
    await edit(cb, *parts)
    if answer:                                 # answer=False — вызывающий ответит своей всплывашкой
        await cb.answer()


@router.callback_query(ClientCB.filter(F.action == "open"))
async def client_open(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    await state.clear()
    await _show_client_card(cb, services, callback_data.client_id)


@router.callback_query(ClientCB.filter(F.action == "edit"))
async def client_edit(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    await state.clear()
    parts = await client_edit_parts(services, callback_data.client_id)
    if parts is None:
        await cb.answer(ui.Toast.no_profile, show_alert=True)
        return
    await edit(cb, *parts)
    await cb.answer()
