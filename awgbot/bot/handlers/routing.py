"""
handlers/routing.py — роутер условной маршрутизации (клиентская часть).

Тумблеры и личный список адресов. Админское разрешение живёт в admin.py: у него
свой гвард роли, и смешивать их в одном роутере значило бы городить проверки
руками там, где за это отвечает фильтр.

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
from awgbot.bot.callbacks import Menu, RoutingCB
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


async def _own(services, client):
    """Клиентская запись говорящего. У админа middleware кладёт client=None —
    достаём его собственный профиль по ADMIN_ID."""
    if client is not None:
        return client
    return await call(services.db.get_client_by_tg, config.ADMIN_ID)


async def _guard(cb: CallbackQuery, services, client) -> bool:
    """Фича доступна этому клиенту? Проверяем на КАЖДОМ действии, а не только при
    отрисовке меню: разрешение мог отозвать админ, пока у человека открыт экран
    со старыми кнопками."""
    if await call(services.routing_client_visible, client):
        return True
    await cb.answer(texts.ROUTING_UNAVAILABLE, show_alert=True)
    return False


async def panel_view(services, client, back_target: str = None):
    """(text, markup) раздела РФ-доступа.

    Вынесено из show_panel, потому что вход бывает не только по колбэку: после
    приёма адресов возвращаемся в тот же раздел НОВЫМ сообщением — редактировать
    там нечего."""
    domains = await call(services.routing_domains, client.id)
    devices = await call(services.routing_device_count, client.id)
    link_ok = await call(services.routing_link_ok)
    text = texts.routing_panel_text(
        master_on=bool(client.routing_master), domains=domains,
        devices=devices, link_ok=link_ok)
    return text, kb.routing_panel(
        client.id, master_on=bool(client.routing_master), domains=domains,
        back_target=back_target or Menu(action="main").pack())


async def show_panel(cb: CallbackQuery, services, client, back_target: str = None):
    """Раздел РФ-доступа: переключатель, охват, личный список.

    back_target параметризован, потому что вход бывает из двух мест: у клиента —
    из главного меню, у админа — из карточки профиля, и возвращать его в
    клиентское меню было бы некуда."""
    text, markup = await panel_view(services, client, back_target)
    await edit(cb, text, markup)


@router.callback_query(RoutingCB.filter(F.action == "panel"))
async def routing_panel(cb: CallbackQuery, client, services, state: FSMContext):
    client = await _own(services, client)
    if not await _guard(cb, services, client):
        return
    await state.clear()
    await show_panel(cb, services, client)
    await cb.answer()


@router.callback_query(RoutingCB.filter(F.action == "master"))
async def routing_master(cb: CallbackQuery, client, services):
    """Мастер-тумблер: «выключить всё разом», не обходя каждое устройство."""
    client = await _own(services, client)
    if not await _guard(cb, services, client):
        return
    new_state = not client.routing_master
    await call(services.set_routing_master, client.id, new_state)
    client = await call(services.db.get_client, client.id)
    await show_panel(cb, services, client)
    await cb.answer("РФ-доступ включён" if new_state else "РФ-доступ выключен")


# ── Личный список адресов ────────────────────────────────────────────────────

@router.callback_query(RoutingCB.filter(F.action == "add"))
async def routing_add_start(cb: CallbackQuery, client, services, state: FSMContext):
    client = await _own(services, client)
    if not await _guard(cb, services, client):
        return
    await state.set_state(RoutingDomains.value)
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
    client = await _own(services, client)
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    await state.clear()
    # Тот же гвард, что и на кнопках. Здесь его не было, и это была дыра: между
    # «пришли адреса» и отправкой списка админ мог отозвать разрешение, а
    # состояние FSM про это не знает — список принимался бы уже у того, кому
    # фича больше не положена.
    if not await call(services.routing_client_visible, client):
        await cleanup_content(message.bot, services, message.chat.id)
        await message.answer(texts.ROUTING_UNAVAILABLE, reply_markup=kb.reply_hide())
        return
    res = await call(services.routing_add_domains, client.id, message.text or "")
    report = texts.routing_add_report(res.added, res.rejected, res.over_limit, res.limit)

    # приглашение «пришли адреса» и вставленный список убираем — они отслужили
    await cleanup_content(message.bot, services, message.chat.id)
    await message.answer(report, reply_markup=kb.reply_hide())
    client = await call(services.db.get_client, client.id)
    text, markup = await panel_view(services, client)
    await send_menu(message, services, text, markup)


@router.callback_query(RoutingCB.filter(F.action == "del"))
async def routing_delete(cb: CallbackQuery, callback_data: RoutingCB, client, services):
    """Удаление по позиции в показанном списке.

    Домен в callback_data не влезает (64 байта на всю строку), поэтому носим
    индекс и перечитываем список на применении: если он успел измениться в
    другом окне, границы не сойдутся и мы ничего не удалим молча наугад.
    """
    client = await _own(services, client)
    if not await _guard(cb, services, client):
        return
    domains = await call(services.routing_domains, client.id)
    idx = callback_data.idx
    if not (0 <= idx < len(domains)):
        await cb.answer("Список изменился — открой заново", show_alert=True)
        await show_panel(cb, services, client)
        return
    removed = domains[idx]
    await call(services.routing_remove_domain, client.id, removed)
    await show_panel(cb, services, client)
    await cb.answer(f"Удалено: {removed}")


@router.callback_query(RoutingCB.filter(F.action == "clear"))
async def routing_clear_ask(cb: CallbackQuery, client, services):
    client = await _own(services, client)
    if not await _guard(cb, services, client):
        return
    await edit(cb, texts.ROUTING_CLEAR_CONFIRM, kb.routing_clear_confirm(client.id))
    await cb.answer()


@router.callback_query(RoutingCB.filter(F.action == "clear_yes"))
async def routing_clear_apply(cb: CallbackQuery, client, services):
    client = await _own(services, client)
    if not await _guard(cb, services, client):
        return
    n = await call(services.routing_clear_domains, client.id)
    await show_panel(cb, services, client)
    await cb.answer(f"Удалено адресов: {n}" if n else "Список и так был пуст")
