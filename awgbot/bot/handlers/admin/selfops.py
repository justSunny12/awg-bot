"""
handlers/admin/selfops.py — личный VPN администратора (AdminSelfCB).
"""

from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import AdminSelfCB, Menu
from awgbot.bot.handlers.common import call, edit, ask_tracked, send_menu
from awgbot.domain.services import BYTES_PER_GB, LimitReached, ServiceError
from awgbot.bot.states import AdminSelfAddDevice
from awgbot.bot.handlers.admin.panel import _return_panel

router = Router(name="admin.selfops")


# ─────────────────────────────────────────────────────────────────────────────
# Личный VPN админа: он тоже пользователь. Работаем с его клиентской записью
# (admin_client), переиспользуя те же тексты/клавиатуры/хелперы, что и клиент.
# Роль остаётся admin, поэтому это отдельные callback'и (AdminSelfCB).
# ─────────────────────────────────────────────────────────────────────────────

async def _self(services):
    """Клиентская запись админа (создаётся при старте; на всякий — гарантируем)."""
    ac = await call(services.admin_client)
    if ac is None:
        await call(services.ensure_admin_client)
        ac = await call(services.admin_client)
    return ac


@router.callback_query(AdminSelfCB.filter(F.action == "devices"))
async def self_devices(cb: CallbackQuery, services):
    ac = await _self(services)
    devices = await call(services.db.list_devices, ac.id)
    slots = await call(services.device_slots, ac.id)
    header = "<b>\U0001F4F1Мои устройства</b>\n\n" + texts.device_slots_line(*slots)
    await edit(cb, header, kb.client_devices(devices))
    await cb.answer()


@router.callback_query(AdminSelfCB.filter(F.action.in_(kb.GEN_ACTIONS)))
async def self_gen_pick(cb: CallbackQuery, callback_data: AdminSelfCB, services):
    """Выбор своего устройства под ссылку/QR/файл — одним обработчиком."""
    ac = await _self(services)
    devices = kb.issuable(await call(services.db.list_devices, ac.id))
    if not devices:
        await cb.answer("Сначала добавь устройство", show_alert=True)
        return
    await edit(cb, kb.PICK_DEVICE_PROMPT[callback_data.action],
               kb.pick_device(devices, callback_data.action))
    await cb.answer()


@router.callback_query(AdminSelfCB.filter(F.action == "add"))
async def self_add_start(cb: CallbackQuery, services, state: FSMContext):
    ac = await _self(services)
    used, limit = await call(services.device_slots, ac.id)
    if limit != 0 and used >= limit:
        await cb.answer("Лимит устройств исчерпан", show_alert=True)
        return
    await state.set_state(AdminSelfAddDevice.name)
    await ask_tracked(cb.message, services, "Введи имя нового устройства:", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(AdminSelfAddDevice.name)
async def self_add_name(message: Message, services, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, "Имя не может быть пустым. Введи ещё раз:")
        return
    await state.update_data(dev_name=name)
    await state.set_state(AdminSelfAddDevice.traffic)
    await ask_tracked(message, services, texts.traffic_limit_device_ask(0), reply_markup=kb.reply_cancel())


@router.message(AdminSelfAddDevice.traffic)
async def self_add_traffic(message: Message, services, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.TRAFFIC_LIMIT_BAD)
        return
    data = await state.get_data()
    name = data.get("dev_name")
    tlimit = int(raw) * BYTES_PER_GB
    await state.clear()
    ac = await _self(services)
    try:
        created = await call(services.add_device, ac.id, name, tlimit)
    except (LimitReached, ServiceError) as e:
        await message.answer(f"Не удалось создать устройство: {e}", reply_markup=kb.reply_hide())
        await _return_panel(message, services)
        return
    await message.answer(f"\u2705 Устройство \u00ab{texts._e(name)}\u00bb создано.",
                         reply_markup=kb.reply_hide())
    dev = await call(services.db.get_device, created.device_id)
    back = Menu(action="main").pack()
    await send_menu(message, services, texts.CONNECT_METHOD_ASK,
                    kb.connect_method_choice(dev.id, back, back_label="⬅️ В меню"))


@router.callback_query(Menu.filter(F.action == "devices"))
async def admin_menu_devices(cb: CallbackQuery, services):
    """«Назад» из карточки устройства (Menu devices). Для админа список
    устройств = его собственные (чужие он смотрит через карточку клиента)."""
    await self_devices(cb, services)
