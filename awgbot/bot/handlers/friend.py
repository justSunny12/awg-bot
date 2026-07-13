"""
handlers/friend.py — роутер роли invited («Друг»).

Друг управляет гостевыми устройствами внутри чужих клиентов: видит инфо про
устройство (имя, онлайн, потребление) + подписку хозяина, умеет выдать ссылку,
файл, вызвать помощь. Ничего структурного (добавить/удалить/пригласить) нельзя,
и лимиты менять нельзя — только просмотр.

МУЛЬТИДРУЖБА: один tg_id может управлять НЕСКОЛЬКИМИ устройствами (разных
клиентов). Если устройство одно — сразу его карточка; если несколько — список,
клик открывает карточку конкретного. Действия несут device_id.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import FriendCB, HelpCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import (call, drop_message, edit, edit_nav, send_device_config,
                             send_menu, send_qr, content_finisher, cleanup_content)

router = Router(name="friend")
# Роль фильтруется НА УРОВНЕ РОУТЕРА (как у client/admin) — новый хендлер
# невозможно забыть защитить. Пер-хендлерные RoleFilter не нужны.
router.message.filter(RoleFilter("invited"))
router.callback_query.filter(RoleFilter("invited"))


async def _device_card(services, dev):
    """Текст+разметка карточки одного устройства друга."""
    host = await call(services.db.get_client, dev.client_id)
    devs = await call(services.friend_devices, dev.friend_tg_id)
    multi = len(devs) > 1
    return texts.friend_panel(dev, host), kb.friend_main(dev.id, multi=multi)


async def friend_panel_payload(services, tg_id: int):
    """Текст+разметка стартового экрана друга: карточка (если устройство одно)
    или список (если несколько). Возвращает (text, markup) или (msg, None)."""
    devs = await call(services.friend_devices, tg_id)
    if not devs:
        return "Устройство не найдено.", None
    if len(devs) == 1:
        return await _device_card(services, devs[0])
    return "<b>📱Твои устройства</b>\n\nВыбери, каким управлять:", kb.friend_device_list(devs)


async def show_friend_panel(target: Message, services, tg_id: int, *, fresh: bool = False):
    """Отрисовать стартовый экран друга (свежим сообщением)."""
    text, markup = await friend_panel_payload(services, tg_id)
    if markup is None:
        return
    await send_menu(target, services, text, markup)


@router.message(CommandStart())
async def friend_start(message: Message, services):
    await show_friend_panel(message, services, message.from_user.id)


@router.callback_query(FriendCB.filter(F.action == "list"))
async def friend_list(cb: CallbackQuery, services):
    """Назад к списку устройств друга (мультидружба)."""
    devs = await call(services.friend_devices, cb.from_user.id)
    if len(devs) == 1:
        text, markup = await _device_card(services, devs[0])
    elif devs:
        text, markup = "<b>📱Твои устройства</b>\n\nВыбери, каким управлять:", kb.friend_device_list(devs)
    else:
        await cb.answer("Устройств нет", show_alert=True)
        return
    await edit_nav(cb, services, text, markup)
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "open"))
async def friend_open(cb: CallbackQuery, callback_data: FriendCB, services):
    """Открыть карточку конкретного устройства друга."""
    dev = await call(services.friend_device_by_id, cb.from_user.id, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    text, markup = await _device_card(services, dev)
    await edit_nav(cb, services, text, markup)
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "refresh"))
async def friend_refresh(cb: CallbackQuery, callback_data: FriendCB, services):
    """Обновить карточку. device_id=0 (безадресные кнопки: «К устройству» из
    завершителя, «Назад» из помощи) → стартовый экран (карточка единственного
    устройства или список при мультидружбе)."""
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    dev = None
    if callback_data.device_id:
        dev = await call(services.friend_device_by_id, cb.from_user.id, callback_data.device_id)
    if dev is None:
        text, markup = await friend_panel_payload(services, cb.from_user.id)
        if markup is None:
            await cb.answer("Устройство не найдено", show_alert=True)
            return
        await edit_nav(cb, services, text, markup)
        await cb.answer()
        return
    text, markup = await _device_card(services, dev)
    await edit_nav(cb, services, text, markup)
    await cb.answer("Обновлено")


@router.callback_query(FriendCB.filter(F.action == "connect_menu"))
async def friend_connect_menu(cb: CallbackQuery, callback_data: FriendCB, services):
    """«Как планируешь подключить устройство?» — назад к карточке устройства."""
    dev = await call(services.friend_device_by_id, cb.from_user.id, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await edit(cb, texts.CONNECT_METHOD_ASK, kb.connect_method_choice_friend(dev.id))
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "gen_link"))
async def friend_gen_link(cb: CallbackQuery, callback_data: FriendCB, services):
    dev = await call(services.friend_device_by_id, cb.from_user.id, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await drop_message(cb)
    try:
        await send_device_config(cb.message, services, dev, "link")
    except Exception as e:                                # noqa: BLE001
        await cb.message.answer(f"Не удалось выдать ссылку: {e}")
    await content_finisher(cb.message, services, texts.finish_link(dev.name), "invited")
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "gen_qr"))
async def friend_gen_qr(cb: CallbackQuery, callback_data: FriendCB, services):
    dev = await call(services.friend_device_by_id, cb.from_user.id, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await drop_message(cb)
    try:
        cfg = await call(services.generate_config, dev.id)
        await send_qr(cb.message, cfg["vpn"], services)
    except Exception as e:                                # noqa: BLE001
        await cb.message.answer(f"Не удалось выдать QR-код: {e}")
    await content_finisher(cb.message, services, texts.finish_qr(dev.name), "invited")
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "gen_file"))
async def friend_gen_file(cb: CallbackQuery, callback_data: FriendCB, services):
    dev = await call(services.friend_device_by_id, cb.from_user.id, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await drop_message(cb)
    try:
        await send_device_config(cb.message, services, dev, "file")
    except Exception as e:                                # noqa: BLE001
        await cb.message.answer(f"Не удалось выдать файл: {e}")
    await content_finisher(cb.message, services, texts.finish_file(dev.name), "invited")
    await cb.answer()


@router.callback_query(FriendCB.filter(F.action == "help"))
async def friend_help(cb: CallbackQuery):
    await edit(cb, "С каким устройством помочь?", kb.friend_help_menu())
    await cb.answer()


@router.callback_query(HelpCB.filter(F.platform.in_(("apple", "android", "windows", "mac"))))
async def friend_help_platform(cb: CallbackQuery, callback_data: HelpCB):
    """Помощь по платформе для друга: инструкция подключения одним сообщением."""
    from awgbot.bot import guides
    guide = callback_data.platform
    steps = [guides.step_text(guide, i) for i in range(guides.step_count(guide))]
    body = "\n\n".join(steps)
    await edit(cb, body, kb.friend_help_back())
    await cb.answer()


__all__ = ["router", "show_friend_panel", "friend_panel_payload"]
