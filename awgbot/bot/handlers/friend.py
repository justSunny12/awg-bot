"""
handlers/friend.py — роутер роли invited: ГОСТЬ (docs/guest-role.md).

Гость — профиль без своей подписки, держит устройства, переданные ему одним
владельцем. Экраны — по образу клиентских: главный (статус сервера, подписка
владельца, счётчик), «Мои устройства», карточка устройства (подключение,
блокировка, удаление), выдача ссылки/QR/файла, помощь. Ничего структурного
(добавить, передать, переименовать) гость не может: имя и слот — у владельца.

В контексте middleware кладёт client = гостевой профиль; устройства — те,
что он держит (list_held_devices). Ownership каждого действия проверяется
по держателю: чужой device_id из старого сообщения — «не найдено».
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import BlockCB, DelDeviceCB, FriendCB, HelpCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import (call, drop_message, edit, edit_nav, send_device_config,
                                        purge_menus, send_menu, content_finisher, cleanup_content)
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.domain.services import ServiceError

router = Router(name="friend")
# Роль фильтруется НА УРОВНЕ РОУТЕРА (как у client/admin) — новый хендлер
# невозможно забыть защитить. Пер-хендлерные RoleFilter не нужны.
router.message.filter(RoleFilter("invited"))
router.callback_query.filter(RoleFilter("invited"))


# ── экраны ───────────────────────────────────────────────────────────────────

async def _held(services, client, device_id: int = 0):
    """Устройства, которые держит гость; с device_id — одно из них или None."""
    devs = await call(services.db.list_held_devices, client.id)
    if not device_id:
        return devs
    return next((d for d in devs if d.id == device_id), None)


async def guest_main_payload(services, client):
    """(text, markup) главного экрана гостя."""
    devs = await _held(services, client)
    donor = await call(services.db.get_client, devs[0].client_id) if devs else None
    server_ok = await call(services.server_ok_cached)
    routing_ok = await call(services.routing_health_for_client, client)   # None — фичи нет
    text = texts.greeting_guest(client.tg_name or client.name, server_ok, donor, len(devs), routing_ok)
    if not devs:
        return text, None          # без устройств кнопкам делать нечего — ждём новый код
    return (text, kb.guest_main(routing_visible=routing_ok is not None,
                                routing_on=await call(services.routing_profile_on, client.id),
                                client_id=client.id))


async def show_guest_main(target: Message, services, client) -> None:
    """Главный экран гостя новым сообщением (через send_menu — прежнее гаснет)."""
    await cleanup_content(target.bot, services, target.chat.id)
    await send_menu(target, services, *await guest_main_payload(services, client))


async def _devices_payload(services, client):
    devs = await _held(services, client)
    return (f"<b>📱 Мои устройства</b>\n\nУ тебя {texts._n_devices(len(devs))}",
            kb.guest_devices(devs))


async def _card_payload(services, dev):
    owner = await call(services.db.get_client, dev.client_id)
    return (texts.held_device_card(dev, int(owner.traffic_limit) if owner else 0),
            kb.held_device_actions(dev, FriendCB(action="list").pack(), cb_cls=FriendCB))


@router.message(CommandStart(deep_link=True))
async def friend_start_with_code(message: Message, command: CommandObject, client, services):
    """/start {код} у гостя: ещё одно устройство от того же владельца или
    переход во владельцы (код клиента). Раньше код здесь молча терялся."""
    from awgbot.bot.handlers.client import take_code_as_member
    await take_code_as_member(message, services, client, (command.args or "").strip())


@router.message(Command("code"))
async def friend_code(message: Message, command: CommandObject, client, services):
    from awgbot.bot.handlers.client import take_code_as_member
    code = (command.args or "").strip()
    if not code:
        await message.answer(texts.CODE_NO_ARG)
        return
    await take_code_as_member(message, services, client, code)


@router.message(CommandStart())
async def friend_start(message: Message, client, services):
    await purge_menus(message.bot, services, message.chat.id)   # /start = заново
    await show_guest_main(message, services, client)


@router.callback_query(FriendCB.filter(F.action == "refresh"))
async def friend_refresh(cb: CallbackQuery, client, services):
    """Главный экран на месте: «В меню» из завершителя, «Назад» из списка."""
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    await edit_nav(cb, services, *await guest_main_payload(services, client))
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "list"))
async def friend_list(cb: CallbackQuery, client, services):
    await edit(cb, *await _devices_payload(services, client))
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "open"))
async def friend_open(cb: CallbackQuery, callback_data: FriendCB, client, services):
    dev = await _held(services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, *await _card_payload(services, dev))
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "connect_menu"))
async def friend_connect_menu(cb: CallbackQuery, callback_data: FriendCB, client, services):
    dev = await _held(services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, texts.CONNECT_METHOD_ASK, kb.connect_method_choice_friend(dev.id))
    await cb.answer()


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
            await edit(cb, kb.PICK_DEVICE_PROMPT[callback_data.action],
                       kb.guest_pick_device(devs, callback_data.action))
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
        await send_device_config(cb.message, services, dev, kind)
    except ServiceError as e:
        # как у клиента: финишер «выше — ссылка» под отказом врал бы; меню под
        # кнопкой уже снято — главный экран следом
        await cb.message.answer(f"Не удалось выдать конфиг: {texts._e(str(e))}")
        await show_guest_main(cb.message, services, client)
        await cb.answer()
        return
    await content_finisher(cb.message, services, texts.finish_config(kind, dev.name), "invited")
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
    await edit(cb, *await _card_payload(services, dev))
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
    await edit(cb, *await _card_payload(services, dev))
    await cb.answer("Разблокировано")


# ── удаление переданного устройства держателем ──────────────────────────────

@router.callback_query(DelDeviceCB.filter(F.stage == "ask"))
async def friend_delete_ask(cb: CallbackQuery, callback_data: DelDeviceCB, client, services):
    dev = await _held(services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, texts.device_delete_by_holder_ask(dev.name),
               kb.confirm_delete_device(dev.id, only=False, guest=True))
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
    # владельцу — что и сколько теперь у него
    if dev.owner_tg_id:
        used, limit = await call(services.device_slots, dev.client_id)
        await notify_one(cb.bot, dev.owner_tg_id,
                         texts.lent_device_deleted_by_holder_notice(dev, used, limit))
    await edit(cb, f"🗑 Устройство «{texts._e(dev.name)}» удалено.", None)
    # профиль гостя живёт и без устройств (список адресов, история — при нём);
    # главный экран сам скажет, что делать дальше
    await send_menu(cb.message, services, *await guest_main_payload(services, client),
                    keep_id=cb.message.message_id)


# ── помощь ───────────────────────────────────────────────────────────────────

@router.callback_query(FriendCB.filter(F.action == "help"))
async def friend_help(cb: CallbackQuery):
    await edit(cb, "С каким устройством помочь?", kb.friend_help_menu())
    await cb.answer()


@router.callback_query(HelpCB.filter(F.platform.in_(("apple", "android", "windows", "mac"))))
async def friend_help_platform(cb: CallbackQuery, callback_data: HelpCB):
    """Помощь по платформе для гостя: инструкция подключения одним сообщением."""
    from awgbot.bot import guides
    guide = callback_data.platform
    steps = [guides.step_text(guide, i) for i in range(guides.step_count(guide))]
    await edit(cb, "\n\n".join(steps), kb.friend_help_back())
    await cb.answer()


__all__ = ["router", "show_guest_main", "guest_main_payload"]
