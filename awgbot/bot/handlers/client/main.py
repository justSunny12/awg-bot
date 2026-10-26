"""main.py — часть пакета client (см. __init__): роутер, главная, /start и активация."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from awgbot.core import config
from awgbot.core import settings
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.filters import RoleFilter
from awgbot.bot.notifier import notify_one
from awgbot.bot.handlers.common import (
    call, cleanup_content, edit_nav, purge_menus, send_menu, show_screen)


router = Router(name="client")
router.message.filter(RoleFilter("client", "activation"))
router.callback_query.filter(RoleFilter("client"))

ADD_FROM_DEVICES = kb.ADD_FROM_DEVICES   # «➕ Устройство» из списка устройств


def _bot_username(services) -> str:
    return getattr(services, "bot_username", "") or ""


# ── главная ──────────────────────────────────────────────────────────────────

async def main_payload(services, client):
    """(текст, клавиатура) главной пользователя."""
    server_ok = await call(services.server_ok_cached)     # 0 exec: статус из state
    slots = await call(services.device_quota, client.id)
    routing_ok = await call(services.routing_health_for_client, client)
    held = await call(services.db.list_held_devices, client.id)
    traffic = await call(services.db.get_client_traffic, client.id)
    routing_visible = await call(services.routing_client_visible, client)
    routing_on = await call(services.routing_profile_on, client.id) if routing_visible else False
    text = texts.greeting_client(client, server_ok, slots, routing_ok, held=held, traffic=traffic,
                                 routing_on=routing_on, bot_username=_bot_username(services))
    used, limit = slots
    markup = kb.client_main(has_devices=used > 0 or bool(held), routing_visible=routing_visible,
                            client_id=client.id, can_add=not limit or used < limit)
    return text, markup


async def _show_main(target, services, client, *, via_edit=None):
    """Главная одним заходом. Держит инвариант «одно активное меню»: новое
    гасит прежнее. Стирает промежуточные служебные сообщения диалога."""
    text, markup = await main_payload(services, client)
    if via_edit is not None:
        await cleanup_content(via_edit.bot, services, via_edit.message.chat.id,
                              keep=via_edit.message.message_id)      # финишер становится главной
        await edit_nav(via_edit, services, text, markup)
    else:
        await cleanup_content(target.bot, services, target.chat.id)
        await send_menu(target, services, text, markup)


# ── /start и активация ───────────────────────────────────────────────────────

@router.message(CommandStart(deep_link=True), RoleFilter("activation"))
async def start_activation(message: Message, command: CommandObject, services, state: FSMContext):
    """Переход по ссылке-инвайту /start {код}."""
    await state.clear()
    code = (command.args or "").strip()
    await _try_activate(message, services, code)


@router.message(CommandStart(), RoleFilter("activation"))
async def start_cold(message: Message, state: FSMContext):
    """Холодный старт: незнакомец нажал «Запустить» без кода-инвайта."""
    await state.clear()
    await message.answer(texts.COLD_START_GREETING)


@router.message(Command("code"), RoleFilter("activation"))
async def code_activation(message: Message, command: CommandObject, services, state: FSMContext):
    await state.clear()
    code = (command.args or "").strip()
    if not code:
        await message.answer(texts.CODE_NO_ARG)
        return
    await _try_activate(message, services, code)


async def _try_activate(message: Message, services, code: str):
    code = code.strip()
    if code[:1] == "F":
        await _activate_friend(message, services, code)
        return
    res = await call(services.activate_client, code, message.from_user.id)
    if not res.ok:
        if res.reason == "already_has_access":
            await message.answer(texts.ACTIVATION_ALREADY)
        else:
            await message.answer(texts.ACTIVATION_INVALID)
        return
    # одно сообщение: «доступ открыт» и сразу выбор устройства для гайда
    await send_menu(message, services, texts.ACTIVATION_OK_HELP, kb.help_menu(is_initial=True))
    u = message.from_user
    handle = f"@{u.username}" if u.username else (u.full_name or str(u.id))
    if settings.get_bool("notifications.client_events.activation", True):
        await notify_one(message.bot, config.ADMIN_ID,
                         texts.activated_admin_notice(res.client, handle, _bot_username(services)))


async def _activate_friend(message: Message, services, code: str):
    """Активация кода друга (F…) незнакомцем: заводится гостевой профиль."""
    u = message.from_user
    res = await call(services.activate_friend, code, u.id, (u.first_name or u.username or ""))
    if not res.ok:
        if res.reason == "already_user":
            await message.answer(texts.FRIEND_ALREADY_USER)
        else:
            await message.answer(texts.ACTIVATION_INVALID)
        return
    # одно сообщение: «тебе передали» и сразу выбор устройства для гайда, как у
    # клиента; «✅ Настрою сам» — на главную гостя
    await purge_menus(message.bot, services, message.chat.id)
    await send_menu(message, services, texts.friend_activated(res.device_name),
                    kb.help_menu(is_initial=True, guest=True))
    await _notify_owner_activated(message, services, res)


async def _notify_owner_activated(message: Message, services, res) -> None:
    dev = await call(services.db.get_device, res.device_id)
    host = await call(services.db.get_client, dev.client_id) if dev else None
    u = message.from_user
    who = texts.tg_link(u.full_name or u.username or str(u.id), u.id, u.username or "")
    if host and host.tg_id:
        await notify_one(message.bot, host.tg_id,
                         texts.friend_activated_host_notice(dev.name, who))


async def take_code_as_member(message: Message, services, client, code: str) -> None:
    """Код от того, у кого уже есть профиль — гость или пользователь.

    F… — ещё одно устройство от того же владельца (иной владелец — отказ, код
    цел). C… — у пользователя отказ «уже есть доступ»; у гостя — переход во
    владельцы с переносом всех переданных устройств.
    """
    code = code.strip()
    u = message.from_user
    if code[:1] == "F":
        res = await call(services.activate_friend, code, u.id, (u.first_name or u.username or ""))
        if not res.ok:
            if res.reason == "other_donor":
                await message.answer(texts.friend_other_donor_refusal(res.held, res.donor))
            elif res.reason == "own_device":
                await message.answer(texts.OWN_DEVICE_CODE)
            else:
                await message.answer(texts.ACTIVATION_INVALID)
            return
        holder = res.holder
        dev = await call(services.db.get_device, res.device_id)
        await message.answer(texts.friend_device_added(dev, res.donor, len(res.held),
                                                       guest=bool(holder.is_guest)))
        if holder.is_guest:
            from awgbot.bot.handlers.friend import show_guest_main
            await show_guest_main(message, services, holder)
        else:
            await _show_main(message, services, holder)
        await _notify_owner_activated(message, services, res)
        return
    if not client.is_guest:
        await message.answer(texts.ACTIVATION_ALREADY)
        await _show_main(message, services, client)
        return
    res = await call(services.activate_client, code, u.id)
    if not res.ok:
        await message.answer(texts.ACTIVATION_INVALID)
        return
    up, new = res.upgrade, res.client
    handle = f"@{u.username}" if u.username else (u.full_name or str(u.id))
    if up is not None and up.moved:
        await message.answer(texts.guest_upgraded(up.donor, up.moved, new.device_limit))
        if up.donor is not None and up.donor.tg_id:
            used, limit = await call(services.device_quota, up.donor.id)
            await notify_one(message.bot, up.donor.tg_id,
                             texts.guest_upgraded_donor_notice(up.moved, new, used, limit))
        admin_text = (texts.activated_admin_notice(new, handle, _bot_username(services))
                      + texts.guest_upgraded_admin_tail(up.donor, up.moved, new.device_limit,
                                                       _bot_username(services)))
    else:
        await message.answer(texts.ACTIVATION_OK)
        admin_text = texts.activated_admin_notice(new, handle, _bot_username(services))
    await _show_main(message, services, new)
    if settings.get_bool("notifications.client_events.activation", True):
        await notify_one(message.bot, config.ADMIN_ID, admin_text)


def parse_link(payload: str) -> tuple[str, int] | None:
    """«sub», «rf», «dev-<id>» → (вид экрана, ref); иначе None (код)."""
    if payload == texts.SUB_PAYLOAD:
        return "sub", 0
    if payload == texts.RF_PAYLOAD_CLIENT:
        return "rf", 0
    head = texts.DEV_PAYLOAD + "-"
    if payload.startswith(head) and payload[len(head):].isdigit():
        return "dev", int(payload[len(head):])
    return None


@router.message(CommandStart(deep_link=True), RoleFilter("client"))
async def start_client_with_code(message: Message, command: CommandObject, client, services,
                                 state: FSMContext):
    """/start {payload} у действующего пользователя: ссылка на экран (подписка,
    РФ-доступ, устройство) или чужое устройство ему в держание."""
    await state.clear()
    payload = (command.args or "").strip()
    link = parse_link(payload)
    if link is not None:
        if not await show_screen(message, services, "client", client, *link):
            await _show_main(message, services, client)
        return
    await take_code_as_member(message, services, client, payload)


@router.message(Command("code"), RoleFilter("client"))
async def code_client(message: Message, command: CommandObject, client, services, state: FSMContext):
    await state.clear()
    code = (command.args or "").strip()
    if not code:
        await message.answer(texts.CODE_NO_ARG)
        return
    await take_code_as_member(message, services, client, code)


@router.message(CommandStart(), RoleFilter("client"))
async def start_client(message: Message, client, services, state: FSMContext):
    await state.clear()
    await purge_menus(message.bot, services, message.chat.id)   # /start = заново
    await _show_main(message, services, client)
