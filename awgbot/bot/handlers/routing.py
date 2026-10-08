"""
handlers/routing.py — роутер условной маршрутизации: раздел профиля.

Один экран «🇷🇺 РФ-доступ»: переключатели устройств (свои и удерживаемые),
«Выбрать все», свои сайты — счётчиком и отдельным экраном «📋 Сайты». Для
клиента и гостя — над СВОИМ профилем, для админа — над любым: своим (вход с
главной) или чужим (из карточки профиля). Админское разрешение — в settings.py.

Профиль для действия берётся в ОДНОМ месте (_profile): клиент — из контекста,
чужой id в кнопке он получить не может; админ — из ref кнопки (middleware
отдаёт ему client=None).

Ключевое свойство фичи: конфиг устройства от переключения НЕ меняется —
тумблер не влечёт ни перевыпуска ссылок, ни предупреждений.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import ClientCB, FriendCB, Menu, RoutingCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import (call, ask_here, back_to_context, edit, role_of)
from awgbot.bot.states import RoutingDomains

router = Router(name="routing")
# Админ тоже пользуется VPN (middleware отдаёт ему role=admin и client=None);
# гость (invited) — субъект своих переданных устройств и своего списка.
router.message.filter(RoleFilter("client", "admin", "invited"))
router.callback_query.filter(RoleFilter("client", "admin", "invited"))


async def _profile(services, client, ref: int = 0):
    """Профиль, над которым действие. Клиент — только свой; админ (client=None)
    — тот, чей id в кнопке, без id — собственный. None — профиля нет."""
    if client is not None:
        return client
    if ref:
        return await call(services.db.get_client, ref)
    return await call(services.db.get_client_by_tg, config.ADMIN_ID)


async def _guard(cb: CallbackQuery, services, client) -> bool:
    """Фича доступна этому профилю? Проверяем на КАЖДОМ действии: разрешение
    мог отозвать админ, пока у человека открыт экран со старыми кнопками."""
    if client is not None and await call(services.routing_client_visible, client):
        return True
    admin = cb.from_user is not None and cb.from_user.id == config.ADMIN_ID
    await cb.answer(texts.ROUTING_NOT_ALLOWED_ADMIN if admin else texts.ROUTING_UNAVAILABLE, show_alert=True)
    return False


def _back_target(client, speaker) -> str:
    """Куда ведёт «Назад» из раздела: клиент и админ в своём — на главную,
    гость — на свою главную, админ в чужом — в карточку того профиля."""
    if speaker is None and client.tg_id != config.ADMIN_ID:
        return ClientCB(action="open", client_id=client.id).pack()
    if getattr(client, "is_guest", False):
        return FriendCB(action="refresh").pack()
    return Menu(action="main").pack()


async def panel_view(services, client, back_target: str, viewer_chat: int | None = None):
    """(text, markup) раздела РФ-доступа."""
    devices = await call(services.routing_devices, client.id)
    enabled, total = await call(services.routing_device_counts, client.id)
    lent_out = await call(services.routing_lent_out, client.id)
    domains = await call(services.routing_domains, client.id)
    link_ok = await call(services.routing_link_ok)
    from awgbot.bot import paging
    text = texts.routing_panel_text(enabled=enabled, total=total, domains=domains,
                                    lent_out=lent_out, link_ok=link_ok)
    return text, kb.routing_panel(
        client.id, devices, lent_out=lent_out, enabled=enabled, total=total,
        n_domains=len(domains), back_target=back_target,
        page=paging.page_of(viewer_chat or client.tg_id, "rtpanel", client.id))


async def sites_view(services, client, viewer_chat: int | None = None):
    """viewer_chat — чат смотрящего: страницу помнит он, а не профиль (админ
    листает чужой список из своего чата)."""
    domains = await call(services.routing_domains, client.id)
    from awgbot.bot import paging
    return (texts.routing_sites_text(domains),
            kb.routing_sites(client.id, domains,
                             page=paging.page_of(viewer_chat or client.tg_id, "rtsites", client.id)))


async def screen_for(services, client, ref: int, kind: str):
    """Экран реестра: kind = rf | sites; client — говорящий (None у админа),
    ref — профиль. None — профиля нет или фича ему не выдана."""
    profile = await _profile(services, client, ref)
    if profile is None or not await call(services.routing_client_visible, profile):
        return None
    viewer = client.tg_id if client is not None else config.ADMIN_ID   # страницу помнит смотрящий
    if kind == "sites":
        return await sites_view(services, profile, viewer)
    return await panel_view(services, profile, _back_target(profile, client), viewer)


async def show_panel(cb: CallbackQuery, services, client, speaker):
    viewer = speaker.tg_id if speaker is not None else config.ADMIN_ID   # страницу помнит смотрящий
    text, markup = await panel_view(services, client, _back_target(client, speaker), viewer)
    await edit(cb, text, markup)


@router.callback_query(RoutingCB.filter(F.action.in_(("panel", "devs"))))
async def routing_panel(cb: CallbackQuery, callback_data: RoutingCB, client, services,
                        state: FSMContext):
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    await state.clear()
    await show_panel(cb, services, profile, client)
    await cb.answer()


@router.callback_query(RoutingCB.filter(F.action == "lent"))
async def routing_lent_row(cb: CallbackQuery):
    await cb.answer("Этим устройством управляет тот, кому оно передано", show_alert=True)


@router.callback_query(RoutingCB.filter(F.action == "dev"))
async def routing_device_toggle(cb: CallbackQuery, callback_data: RoutingCB,
                                client, services):
    """Переключить режим на одном устройстве. В ref — device_id; профиль
    достаём через устройство. Экран перерисовывается на месте."""
    dev = await call(services.db.get_device, callback_data.ref)
    if dev is None:
        await cb.answer(texts.ROUTING_UNAVAILABLE, show_alert=True)
        return
    subject_id = dev.holder_client_id if dev.is_lent else dev.client_id
    if client is not None and subject_id != client.id:
        await cb.answer(texts.ROUTING_UNAVAILABLE, show_alert=True)
        return
    if client is None and dev.is_lent:
        await cb.answer("Этим устройством управляет тот, кому оно передано", show_alert=True)
        return
    profile = await _profile(services, client, subject_id)
    if not await _guard(cb, services, profile):
        return
    new_state = await call(services.toggle_routing_device, dev.id)
    await show_panel(cb, services, profile, client)
    await cb.answer("включено" if new_state else "выключено")


@router.callback_query(RoutingCB.filter(F.action == "all"))
async def routing_all_toggle(cb: CallbackQuery, callback_data: RoutingCB, client, services):
    """Массовый выбор: ☑️ — довести до всех; ✅ (включены все) — снять все."""
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    enabled, total = await call(services.routing_device_counts, profile.id)
    if not total:
        await cb.answer("Устройств пока нет", show_alert=True)
        return
    turn_on = enabled < total
    await call(services.set_routing_all, profile.id, turn_on)
    await show_panel(cb, services, profile, client)
    await cb.answer("Включено на всех" if turn_on else "Выключено на всех")


# ── свои сайты ───────────────────────────────────────────────────────────────

@router.callback_query(RoutingCB.filter(F.action == "sites"))
async def routing_sites(cb: CallbackQuery, callback_data: RoutingCB, client, services,
                        state: FSMContext):
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    await state.clear()
    await edit(cb, *await sites_view(services, profile, cb.message.chat.id))
    await cb.answer()


@router.callback_query(RoutingCB.filter(F.action == "add"))
async def routing_add_start(cb: CallbackQuery, callback_data: RoutingCB, client, services,
                            state: FSMContext):
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    await state.set_state(RoutingDomains.value)
    # чей список пополняем, помнит диалог: у админа контекст — не он сам;
    # куда вернуться по «Отмене» и итогу — откуда пришли: раздел или «Сайты»
    origin = "rf" if callback_data.tag == "panel" else "sites"
    await ask_here(cb, services, state, texts.ROUTING_ADD_PROMPT, origin, profile.id,
                   rt_client=profile.id)
    await cb.answer()


# Команды во время ввода «➕ Сайт»: роутер маршрутизации стоит раньше клиентского,
# и /start или /code ушли бы в список сайтов. Сброс ввода и обычная обработка —
# как у гостя, чей роутер стоит раньше этого.
@router.message(RoutingDomains.value, CommandStart(deep_link=True), RoleFilter("client"))
async def routing_input_start_payload(message: Message, command: CommandObject, client, services,
                                      state: FSMContext):
    from awgbot.bot.handlers import client as ch
    await ch.start_client_with_code(message, command, client, services, state)


@router.message(RoutingDomains.value, CommandStart(), RoleFilter("client"))
async def routing_input_start(message: Message, client, services, state: FSMContext):
    from awgbot.bot.handlers import client as ch
    await ch.start_client(message, client, services, state)


@router.message(RoutingDomains.value, Command("code"), RoleFilter("client"))
async def routing_input_code(message: Message, command: CommandObject, client, services, state: FSMContext):
    from awgbot.bot.handlers import client as ch
    await ch.code_client(message, command, client, services, state)


@router.message(RoutingDomains.value)
async def routing_add_apply(message: Message, client, services, state: FSMContext):
    """Приём пачки: итог — первой строкой экрана «Сайты», приглашение и
    вставленный список убираются."""
    data = await state.get_data()
    ref = int(data.get("rt_client") or 0)
    profile = await _profile(services, client, ref)
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    await state.clear()
    role = role_of(client)
    # тот же гвард, что и на кнопках: разрешение могли отозвать во время ввода
    if profile is None or not await call(services.routing_client_visible, profile):
        await back_to_context(message, services, {}, role, client, note="⚠️ " + texts.ROUTING_UNAVAILABLE)
        return
    res = await call(services.routing_add_domains, profile.id, message.text or "")
    report = texts.routing_add_report(res.added, res.rejected, res.over_limit, res.limit)
    await back_to_context(message, services, data, role, client, note=report)


@router.callback_query(RoutingCB.filter(F.action == "del"))
async def routing_delete(cb: CallbackQuery, callback_data: RoutingCB, client, services):
    """Удаление по позиции в показанном списке — сразу, итог всплывашкой.
    Домен в callback_data не влезает (64 байта), поэтому носим индекс и
    перечитываем список: изменился — ничего не удаляем наугад."""
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    domains = await call(services.routing_domains, profile.id)
    idx = callback_data.idx
    if not (0 <= idx < len(domains)) or callback_data.tag != kb.entry_tag(domains[idx]):
        await cb.answer("Список изменился — открой раздел заново", show_alert=True)
        await edit(cb, *await sites_view(services, profile, cb.message.chat.id))
        return
    removed = domains[idx]
    await call(services.routing_remove_domain, profile.id, removed)
    await cb.answer(texts.routing_domain_removed(removed))
    await edit(cb, *await sites_view(services, profile, cb.message.chat.id))


@router.callback_query(RoutingCB.filter(F.action == "clear"))
async def routing_clear_ask(cb: CallbackQuery, callback_data: RoutingCB, client, services):
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    n = len(await call(services.routing_domains, profile.id))
    await edit(cb, texts.routing_clear_ask(n), kb.routing_clear_confirm(profile.id))
    await cb.answer()


@router.callback_query(RoutingCB.filter(F.action == "clear_yes"))
async def routing_clear_apply(cb: CallbackQuery, callback_data: RoutingCB, client, services):
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    n = await call(services.routing_clear_domains, profile.id)
    await edit(cb, *await sites_view(services, profile, cb.message.chat.id))
    await cb.answer(f"Удалено адресов: {n}" if n else "Список и так был пуст")


__all__ = ["router", "panel_view", "sites_view", "screen_for"]
