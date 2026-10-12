"""create.py — часть пакета clients (см. __init__): новый профиль: имя → устройства → трафик → срок, приглашение."""
from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot.callbacks import Menu, PeriodCB, PresetCB
from awgbot.bot.handlers.common import (
    call, edit, ask_here, ask_tracked, cleanup_content, send_menu, drop_message)
from awgbot.domain.services import BYTES_PER_GB, ServiceError
from awgbot.bot.states import CreateClient
from awgbot.bot.handlers.admin.panel import _return_panel, _bot
from .card import router


# ─────────────────────────────────────────────────────────────────────────────
# Новый профиль: имя → устройства → трафик → срок (пресетами)
# ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(Menu.filter(F.action == "add_client"))
async def add_client_start(cb: CallbackQuery, services, state: FSMContext):
    await state.set_state(CreateClient.name)
    await ask_here(cb, services, state, texts.NEW_PROFILE_NAME, "main")
    await cb.answer()


@router.message(CreateClient.name)
async def add_client_name(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    await state.update_data(name=name)
    await state.set_state(CreateClient.limit)
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, texts.new_profile_devs(name), kb.new_profile_devs_kb())


@router.callback_query(PresetCB.filter(F.kind == "new_devs"), CreateClient.limit)
async def add_client_devs_preset(cb: CallbackQuery, callback_data: PresetCB, services, state: FSMContext):
    data = await state.get_data()
    name = data.get("name") or ""
    if callback_data.val < 0:
        await ask_here(cb, services, state, texts.OTHER_NUMBER_PROMPT, "main")
        await cb.answer()
        return
    await _devs_chosen(cb.message, services, state, name, int(callback_data.val), via_cb=cb)


async def _devs_chosen(message: Message, services, state: FSMContext, name: str, limit: int,
                       via_cb: CallbackQuery | None = None) -> None:
    await state.update_data(limit=limit)
    await state.set_state(CreateClient.traffic)
    text, markup = texts.new_profile_traffic(name, limit), kb.new_profile_traffic_kb()
    if via_cb is not None:
        await edit(via_cb, text, markup)
        await via_cb.answer()
        return
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, text, markup)


@router.message(CreateClient.limit)
async def add_client_limit(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD_LIMIT)
        return
    name = (await state.get_data()).get("name") or ""
    await _devs_chosen(message, services, state, name, int(raw))


@router.callback_query(PresetCB.filter(F.kind == "new_traffic"), CreateClient.traffic)
async def add_client_traffic_preset(cb: CallbackQuery, callback_data: PresetCB, services, state: FSMContext):
    data = await state.get_data()
    if callback_data.val < 0:
        await ask_here(cb, services, state, texts.OTHER_NUMBER_PROMPT, "main")
        await cb.answer()
        return
    await _traffic_chosen(cb.message, services, state, data, int(callback_data.val), via_cb=cb)


async def _traffic_chosen(message: Message, services, state: FSMContext, data: dict, gb_value: int,
                          via_cb: CallbackQuery | None = None) -> None:
    await state.update_data(traffic_gb=gb_value)
    name, limit = data.get("name") or "", int(data.get("limit") or 0)
    text = texts.new_profile_period(name, limit, gb_value)
    markup = kb.period_kb("create")
    if via_cb is not None:
        await edit(via_cb, text, markup)
        await via_cb.answer()
        return
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, text, markup)


@router.message(CreateClient.traffic)
async def add_client_traffic(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.NUMBER_BAD_LIMIT)
        return
    await _traffic_chosen(message, services, state, await state.get_data(), int(raw))


@router.callback_query(PeriodCB.filter(F.ctx == "create"))
async def add_client_period(cb: CallbackQuery, callback_data: PeriodCB, services, state: FSMContext):
    data = await state.get_data()
    name = data.get("name")
    limit = data.get("limit")
    traffic_gb = data.get("traffic_gb")
    await state.clear()
    if not name or limit is None or traffic_gb is None:  # limit/traffic=0 валидны
        await cb.answer("Диалог устарел — открой создание профиля заново", show_alert=True)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                              # noqa: BLE001
            pass
        await _return_panel(cb.message, services)
        return
    try:
        created = await call(services.create_client, name, int(limit), callback_data.kind,
                             int(traffic_gb) * BYTES_PER_GB)
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
        return
    client = await call(services.db.get_client, created.client_id)
    await cb.answer()
    await drop_message(cb, services)
    await _invite_menu(cb.message, services, client, created.invite_code, new=True)


async def _invite_menu(message: Message, services, client, code: str, *, new: bool) -> None:
    """Приглашение одним сообщением-меню: текст для пересылки, черта, сводка
    профиля; кнопки отправки и копирования несут только текст приглашения,
    выходы — в карточку и на главную (сообщение при этом перерисовывается)."""
    bot = _bot(services) or (await message.bot.me()).username
    link = f"https://t.me/{bot}?start={code}"
    await send_menu(message, services, texts.invite_screen(link, client, new=new),
                    kb.invite_menu(texts.invite_plain(link), link, client.id))
