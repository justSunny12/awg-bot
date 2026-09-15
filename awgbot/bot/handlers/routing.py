"""
handlers/routing.py — роутер условной маршрутизации: раздел профиля.

Тумблеры устройств и личный список адресов — для клиента над СВОИМ профилем,
для админа над ЛЮБЫМ: своим (вход с главной) или чужим (вход из карточки
профиля при разборе проблемы). Админское разрешение живёт в settings.py.

Профиль для действия берётся в ОДНОМ месте (_profile): клиент — из контекста,
чужой id в кнопке он получить не может; админ — из ref кнопки (middleware
отдаёт ему client=None). Раньше ref у админа учитывали только четыре действия,
а список адресов правился всегда у него самого, из чьей бы панели он ни жал.

Ключевое свойство фичи, из которого следует вся простота этих хендлеров: конфиг
устройства от переключения НЕ меняется. Поэтому тумблер не влечёт ни перевыпуска
ссылок, ни предупреждений — щёлкнул и щёлкнул.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import ClientCB, Menu, RoutingCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import (call, ask_tracked, cleanup_content,
                                        edit, send_menu)
from awgbot.bot.states import RoutingDomains

router = Router(name="routing")
# Админ тоже пользуется VPN, а middleware отдаёт ему role=admin и client=None.
# Без него в фильтре у админа не было бы ни мастер-тумблера, ни списка адресов —
# только право раздавать доступ другим.
router.message.filter(RoleFilter("client", "admin"))
router.callback_query.filter(RoleFilter("client", "admin"))


async def _profile(services, client, ref: int = 0):
    """Профиль, над которым действие. Клиент — только свой; админ (client=None)
    — тот, чей id в кнопке, без id — собственный. None — профиля нет."""
    if client is not None:
        return client
    if ref:
        return await call(services.db.get_client, ref)
    return await call(services.db.get_client_by_tg, config.ADMIN_ID)


async def _guard(cb: CallbackQuery, services, client) -> bool:
    """Фича доступна этому профилю? Проверяем на КАЖДОМ действии, а не только при
    отрисовке меню: разрешение мог отозвать админ, пока у человека открыт экран
    со старыми кнопками. Для чужого профиля у админа — та же проверка: без
    разрешения раздела у профиля нет, и править его нечего."""
    if client is not None and await call(services.routing_client_visible, client):
        return True
    await cb.answer(texts.ROUTING_UNAVAILABLE, show_alert=True)
    return False


def _back_target(client, speaker) -> str:
    """Куда ведёт «Назад» из раздела. Клиент и админ в своём разделе — на
    главную (карточки админа в списке профилей нет). Админ в чужом — в карточку
    того профиля, откуда пришёл."""
    if speaker is None and client.tg_id != config.ADMIN_ID:
        return ClientCB(action="open", client_id=client.id).pack()
    return Menu(action="main").pack()


async def panel_view(services, client, back_target: str):
    """(text, markup) раздела РФ-доступа.

    Вынесено из show_panel, потому что вход бывает не только по колбэку: после
    приёма адресов возвращаемся в тот же раздел НОВЫМ сообщением — редактировать
    там нечего."""
    domains = await call(services.routing_domains, client.id)
    enabled, total = await call(services.routing_device_counts, client.id)
    link_ok = await call(services.routing_link_ok)
    on = enabled > 0
    text = texts.routing_panel_text(
        master_on=on, domains=domains,
        enabled=enabled, total=total, link_ok=link_ok)
    return text, kb.routing_panel(
        client.id, master_on=on, domains=domains,
        enabled=enabled, total=total, back_target=back_target)


async def show_panel(cb: CallbackQuery, services, client, speaker):
    """Раздел РФ-доступа: переключатель, охват, личный список."""
    text, markup = await panel_view(services, client, _back_target(client, speaker))
    await edit(cb, text, markup)


@router.callback_query(RoutingCB.filter(F.action == "panel"))
async def routing_panel(cb: CallbackQuery, callback_data: RoutingCB, client, services,
                        state: FSMContext):
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    await state.clear()
    await show_panel(cb, services, profile, client)
    await cb.answer()


async def devices_view(services, client):
    """(text, markup) экрана устройств профиля."""
    devices = await call(services.routing_devices, client.id)
    enabled, total = await call(services.routing_device_counts, client.id)
    return texts.routing_devices_text(enabled, total), kb.routing_devices(
        client.id, devices,
        back_target=RoutingCB(action="panel", ref=client.id).pack())


async def _show_devices(cb: CallbackQuery, services, client) -> None:
    text, markup = await devices_view(services, client)
    await edit(cb, text, markup)


@router.callback_query(RoutingCB.filter(F.action == "devs"))
async def routing_devices_screen(cb: CallbackQuery, callback_data: RoutingCB, client,
                                 services):
    """Экран устройств: переключатели по одному плюс массовое действие."""
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    await _show_devices(cb, services, profile)
    await cb.answer()


@router.callback_query(RoutingCB.filter(F.action == "dev"))
async def routing_device_toggle(cb: CallbackQuery, callback_data: RoutingCB,
                                client, services):
    """Переключить режим на одном устройстве. В ref здесь device_id, а не
    client_id, — профиль достаём через устройство. Экран перерисовывается на
    месте."""
    dev = await call(services.db.get_device, callback_data.ref)
    # Чужое устройство у клиента — отказ молча по существу: колбэк мог прийти
    # из старого сообщения, а подтверждать чужой id ответом «нет такого» незачем.
    if dev is None or (client is not None and dev.client_id != client.id):
        await cb.answer(texts.ROUTING_UNAVAILABLE, show_alert=True)
        return
    profile = await _profile(services, client, dev.client_id)
    if not await _guard(cb, services, profile):
        return
    new_state = await call(services.toggle_routing_device, dev.id)
    await _show_devices(cb, services, profile)
    await cb.answer("включено" if new_state else "выключено")


@router.callback_query(RoutingCB.filter(F.action == "all"))
async def routing_all_toggle(cb: CallbackQuery, callback_data: RoutingCB, client, services):
    """Массовое действие. Направление ВЫВОДИМ из состояния: выключить всё
    осмысленно, только когда включено уже всё; в остальных случаях полезнее
    довести набор до полного."""
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    enabled, total = await call(services.routing_device_counts, profile.id)
    if not total:
        await cb.answer("Устройств пока нет", show_alert=True)
        return
    # Не «включено ноль», а «включено не всё»: подпись кнопки гласит
    # «включить все», пока хоть одно выключено, и действие обязано ей
    # соответствовать.
    turn_on = enabled < total
    await call(services.set_routing_all, profile.id, turn_on)
    await _show_devices(cb, services, profile)
    await cb.answer("Включено на всех" if turn_on else "Выключено на всех")


# ── Личный список адресов ────────────────────────────────────────────────────

@router.callback_query(RoutingCB.filter(F.action == "add"))
async def routing_add_start(cb: CallbackQuery, callback_data: RoutingCB, client, services,
                            state: FSMContext):
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    await state.set_state(RoutingDomains.value)
    # Чей список пополняем, помнит диалог: на шаге приёма текста кнопки с id
    # профиля уже нет, а у админа контекст — не он сам.
    await state.update_data(rt_client=profile.id)
    await ask_tracked(cb.message, services, texts.ROUTING_ADD_PROMPT,
                      reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(RoutingDomains.value)
async def routing_add_apply(message: Message, client, services, state: FSMContext):
    """Приём пачки. Разбор показываем построчно: человек вставляет списком, и
    молча взять половину — оставить его гадать, почему добавилось меньше.

    Отчёт печатаем НАД разделом и возвращаемся в него же: иначе диалог кончался
    сообщением без единой кнопки, а приглашение «пришли адреса» так и висело в
    чате."""
    ref = int((await state.get_data()).get("rt_client") or 0)
    profile = await _profile(services, client, ref)
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    await state.clear()
    # Тот же гвард, что и на кнопках. Здесь его не было, и это была дыра: между
    # «пришли адреса» и отправкой списка админ мог отозвать разрешение, а
    # состояние FSM про это не знает — список принимался бы уже у того, кому
    # фича больше не положена.
    if profile is None or not await call(services.routing_client_visible, profile):
        await cleanup_content(message.bot, services, message.chat.id)
        await message.answer(texts.ROUTING_UNAVAILABLE, reply_markup=kb.reply_hide())
        return
    res = await call(services.routing_add_domains, profile.id, message.text or "")
    report = texts.routing_add_report(res.added, res.rejected, res.over_limit, res.limit)

    # приглашение «пришли адреса» и вставленный список убираем — они отслужили
    await cleanup_content(message.bot, services, message.chat.id)
    await message.answer(report, reply_markup=kb.reply_hide())
    profile = await call(services.db.get_client, profile.id)
    text, markup = await panel_view(services, profile, _back_target(profile, client))
    await send_menu(message, services, text, markup)


@router.callback_query(RoutingCB.filter(F.action == "del"))
async def routing_delete(cb: CallbackQuery, callback_data: RoutingCB, client, services):
    """Удаление по позиции в показанном списке.

    Домен в callback_data не влезает (64 байта на всю строку), поэтому носим
    индекс и перечитываем список на применении: если он успел измениться в
    другом окне, границы не сойдутся и мы ничего не удалим молча наугад.
    """
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    domains = await call(services.routing_domains, profile.id)
    idx = callback_data.idx
    if not (0 <= idx < len(domains)):
        await cb.answer("Список изменился — открой заново", show_alert=True)
        await show_panel(cb, services, profile, client)
        return
    removed = domains[idx]
    await call(services.routing_remove_domain, profile.id, removed)
    await show_panel(cb, services, profile, client)
    await cb.answer(f"Удалено: {removed}")


@router.callback_query(RoutingCB.filter(F.action == "clear"))
async def routing_clear_ask(cb: CallbackQuery, callback_data: RoutingCB, client, services):
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    await edit(cb, texts.ROUTING_CLEAR_CONFIRM, kb.routing_clear_confirm(profile.id))
    await cb.answer()


@router.callback_query(RoutingCB.filter(F.action == "clear_yes"))
async def routing_clear_apply(cb: CallbackQuery, callback_data: RoutingCB, client, services):
    profile = await _profile(services, client, callback_data.ref)
    if not await _guard(cb, services, profile):
        return
    n = await call(services.routing_clear_domains, profile.id)
    await show_panel(cb, services, profile, client)
    await cb.answer(f"Удалено адресов: {n}" if n else "Список и так был пуст")
