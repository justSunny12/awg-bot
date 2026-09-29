"""
handlers/friend.py — роутер роли invited: ГОСТЬ.

Гость — профиль без своей подписки, держит устройства, переданные ему одним
владельцем. Экраны — по образу клиентских: главный (VPN и РФ-доступ, подписка
владельца, трафик), «📱 Устройства», карточка устройства (выдача, блокировка,
удаление), помощь с теми же гайдами, что у клиента. Ничего структурного
(добавить, передать, переименовать) гость не может: имя и слот — у владельца.

В контексте middleware кладёт client = гостевой профиль; устройства — те,
что он держит (list_held_devices). Ownership каждого действия проверяется
по держателю: чужой device_id из старого сообщения — «не найдено».
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import BlockCB, DelDeviceCB, FriendCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import (call, drop_message, edit, edit_nav, send_device_config,
                                        purge_menus, send_menu, cleanup_content, show_screen)
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.domain.services import ServiceError

router = Router(name="friend")
router.message.filter(RoleFilter("invited"))
router.callback_query.filter(RoleFilter("invited"))


# ── экраны ───────────────────────────────────────────────────────────────────

async def _held(services, client, device_id: int = 0):
    """Устройства, которые держит гость; с device_id — одно из них или None."""
    devs = await call(services.db.list_held_devices, client.id)
    if not device_id:
        return devs
    return next((d for d in devs if d.id == device_id), None)


async def held_one(services, client, device_id: int):
    return await _held(services, client, device_id)


async def guest_main_payload(services, client):
    """(text, markup) главного экрана гостя."""
    devs = await _held(services, client)
    donor = await call(services.db.get_client, devs[0].client_id) if devs else None
    server_ok = await call(services.server_ok_cached)
    routing_ok = await call(services.routing_health_for_client, client)   # None — фичи нет
    routing_on = await call(services.routing_profile_on, client.id) if routing_ok is not None else False
    text = texts.greeting_guest(client.tg_name or client.name, server_ok, donor, devs, routing_ok,
                                routing_on=routing_on,
                                bot_username=getattr(services, "bot_username", "") or "")
    return (text, kb.guest_main(routing_visible=routing_ok is not None, client_id=client.id,
                                has_devices=bool(devs)))


async def show_guest_main(target: Message, services, client) -> None:
    """Главный экран гостя новым сообщением (через send_menu — прежнее гаснет)."""
    await cleanup_content(target.bot, services, target.chat.id)
    await send_menu(target, services, *await guest_main_payload(services, client))


async def devices_payload(services, client, chat_id: int = 0):
    devs = await _held(services, client)
    from awgbot.bot import paging
    return (texts.devices_header(len(devs), 0, devs, guest=True),
            kb.guest_devices(devs, page=paging.page_of(chat_id or client.tg_id, "gdevices")))


_devices_payload = devices_payload


async def card_payload(services, dev):
    owner = await call(services.db.get_client, dev.client_id)
    return (texts.device_card_held(dev, int(owner.traffic_limit) if owner else 0),
            kb.held_device_actions(dev, FriendCB(action="list").pack(), cb_cls=FriendCB))


_card_payload = card_payload


@router.message(CommandStart(deep_link=True))
async def friend_start_with_code(message: Message, command: CommandObject, client, services,
                                 state: FSMContext):
    """/start {payload} у гостя: ссылка на экран (РФ-доступ, устройство), ещё
    одно устройство от того же владельца или переход во владельцы."""
    from awgbot.bot.handlers.client import parse_link, take_code_as_member
    await state.clear()                       # иначе текст после «➕ Сайт» ушёл бы в routing_add_apply
    payload = (command.args or "").strip()
    link = parse_link(payload)
    if link is not None:
        if not await show_screen(message, services, "invited", client, *link):
            await show_guest_main(message, services, client)
        return
    await take_code_as_member(message, services, client, payload)


@router.message(Command("code"))
async def friend_code(message: Message, command: CommandObject, client, services, state: FSMContext):
    from awgbot.bot.handlers.client import take_code_as_member
    await state.clear()
    code = (command.args or "").strip()
    if not code:
        await message.answer(texts.CODE_NO_ARG)
        return
    await take_code_as_member(message, services, client, code)


@router.message(CommandStart())
async def friend_start(message: Message, client, services, state: FSMContext):
    await state.clear()
    await purge_menus(message.bot, services, message.chat.id)   # /start = заново
    await show_guest_main(message, services, client)


@router.callback_query(FriendCB.filter(F.action == "refresh"))
async def friend_refresh(cb: CallbackQuery, client, services, state: FSMContext):
    """Главный экран на месте: «В меню» из завершителя, «Назад» из списка."""
    await state.clear()
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    await edit_nav(cb, services, *await guest_main_payload(services, client))
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "list"))
async def friend_list(cb: CallbackQuery, client, services):
    await edit(cb, *await devices_payload(services, client, cb.message.chat.id))
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "open"))
async def friend_open(cb: CallbackQuery, callback_data: FriendCB, client, services):
    dev = await _held(services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, *await card_payload(services, dev))
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "connect_menu"))
async def friend_connect_menu(cb: CallbackQuery, callback_data: FriendCB, client, services):
    """Кнопка старого образца — карточка с рядом выдачи."""
    await friend_open(cb, callback_data, client, services)


@router.callback_query(FriendCB.filter(F.action.in_(kb.GEN_ACTIONS)))
async def friend_gen(cb: CallbackQuery, callback_data: FriendCB, client, services):
    """Ссылка/QR/файл. device_id=0 — с главного экрана: одно устройство —
    сразу выдача, несколько — выбор."""
    devs = await _held(services, client)
    if not callback_data.device_id:
        if not devs:
            await cb.answer("Устройств нет", show_alert=True)
            return
        if len(devs) > 1:
            from awgbot.bot import paging
            await edit(cb, kb.PICK_DEVICE_PROMPT[callback_data.action],
                       kb.guest_pick_device(devs, callback_data.action,
                                            page=paging.page_of(cb.message.chat.id, "gpick")))
            await cb.answer()
            return
        dev = devs[0]
    else:
        dev = next((d for d in devs if d.id == callback_data.device_id), None)
        if dev is None:
            await cb.answer("Устройство не найдено", show_alert=True)
            return
    kind = kb.gen_kind(callback_data.action)
    await drop_message(cb)
    try:
        await send_device_config(cb.message, services, dev, kind, finisher=kb.friend_finisher())
    except ServiceError as e:
        await cb.message.answer(f"Не удалось выдать конфиг: {texts._e(str(e))}")
        await show_guest_main(cb.message, services, client)
    await cb.answer()


# ── блокировка своим битом (с подтверждением) ────────────────────────────────

@router.callback_query(BlockCB.filter(F.action == "menu_block"))
async def friend_block_ask(cb: CallbackQuery, callback_data: BlockCB, client, services):
    dev = await _held(services, client, callback_data.ref) if callback_data.target == "dev" else None
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, texts.block_device_ask(dev.name), kb.block_device_confirm(dev.id, guest=True))
    await cb.answer()


@router.callback_query(BlockCB.filter(F.action == "block"))
async def friend_block_do(cb: CallbackQuery, callback_data: BlockCB, client, services):
    from awgbot.core.blocks import DeviceBlock
    dev = await _held(services, client, callback_data.ref) if callback_data.target == "dev" else None
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    notes = await call(services.block_device_manual, dev.id, DeviceBlock.USER, True)
    await send_notifications(cb.bot, notes)
    dev = await call(services.db.get_device, dev.id)
    await edit(cb, *await card_payload(services, dev))
    await cb.answer("Заблокировано")


@router.callback_query(BlockCB.filter(F.action == "menu_unblock"))
async def friend_unblock(cb: CallbackQuery, callback_data: BlockCB, client, services):
    from awgbot.core.blocks import DeviceBlock
    dev = await _held(services, client, callback_data.ref) if callback_data.target == "dev" else None
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    if not (int(dev.block_reason) & int(DeviceBlock.USER)):
        await cb.answer("Ты не блокировал это устройство", show_alert=True)
        return
    notes = await call(services.unblock_device_manual, dev.id, DeviceBlock.USER, True)
    await send_notifications(cb.bot, notes)
    dev = await call(services.db.get_device, dev.id)
    await edit(cb, *await card_payload(services, dev))
    await cb.answer("Разблокировано")


# ── удаление переданного устройства держателем ──────────────────────────────

@router.callback_query(DelDeviceCB.filter(F.stage == "ask"))
async def friend_delete_ask(cb: CallbackQuery, callback_data: DelDeviceCB, client, services):
    dev = await _held(services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, texts.device_delete_ask(dev, held=True),
               kb.confirm_delete_device(dev.id, guest=True))
    await cb.answer()


@router.callback_query(DelDeviceCB.filter(F.stage == "confirm"))
async def friend_delete_confirm(cb: CallbackQuery, callback_data: DelDeviceCB, client, services):
    dev = await _held(services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    try:
        await call(services.remove_device, dev.id)          # держатель удалил сам —
    except ServiceError as e:                               # «удалено владельцем» ему не шлём
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer()
    if dev.owner_tg_id:
        used, limit = await call(services.device_slots, dev.client_id)
        await notify_one(cb.bot, dev.owner_tg_id,
                         texts.lent_device_deleted_by_holder_notice(dev, used, limit))
    await edit(cb, f"🗑 {texts._e(dev.name)} удалено", None)
    await send_menu(cb.message, services, *await guest_main_payload(services, client),
                    keep_id=cb.message.message_id)


# ── помощь: те же гайды, что у клиента (handlers/guide.py) ───────────────────

@router.callback_query(FriendCB.filter(F.action == "help"))
async def friend_help(cb: CallbackQuery):
    await edit(cb, texts.HELP_INTRO, kb.help_menu(guest=True))
    await cb.answer()


__all__ = ["router", "show_guest_main", "guest_main_payload", "devices_payload", "card_payload",
           "held_one"]
