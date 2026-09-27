"""
screens.py — реестр экранов: (вид, ref) → (текст, клавиатура).

Одна точка для трёх входов, которым нужен экран без нажатия на его кнопку:
возврат после текстового ввода и «✖️ Отмена» (handlers/common.py: ask_here,
back_to_context), ссылки /start <payload> в текстах, кнопки в уведомлениях.
Сборщики экранов живут в роутерах ролей; реестр их только зовёт, поэтому
импорты ленивые — модуль не тянет хендлеры при загрузке.

Виды (kind):
  main     главная роли (admin / client / invited)
  devices  «📱 Устройства» клиента и гостя
  dev      карточка устройства (ref — id устройства; клиент: своё или
           удерживаемое, гость: удерживаемое)
  sub      «💳 Подписка» клиента
  rf       «🇷🇺 РФ-доступ» (ref — id профиля; у клиента и гостя — свой)
  sites    «📋 Сайты» РФ-доступа (ref — id профиля)
  guide    шаг 0 гайда подключения (ref — вариант: 0 connect, 1 connect_apple)
  админ:   cl (карточка профиля), edit («✏️ Изменить»), clients, extend
           (экран продления), dev, devices («📱 Мои устройства»), online,
           expiring, unassigned, traffic, traffic_dev (ref — профиль), gw
           (карточка слота), upd (раздел обновлений)

note — итог только что сделанного: первой строкой экрана, до следующего
перехода («✅ Имя устройства: A → B»).
"""

from __future__ import annotations


def with_note(text: str, note: str) -> str:
    return f"{note}\n\n{text}" if note else text


async def render(kind: str, ref: int = 0, *, services, role: str, client=None,
                 chat_id: int = 0, note: str = ""):
    """(text, markup) экрана или None, если экрана нет (чужой ref, роль без
    такого экрана). Ошибок наружу не бросает — вызывающий подставит главную."""
    try:
        parts = await _render(kind, int(ref or 0), services, role, client, chat_id)
    except Exception:                                  # noqa: BLE001
        return None
    if parts is None:
        return None
    text, markup = parts
    return with_note(text, note), markup


async def _render(kind: str, ref: int, services, role: str, client, chat_id: int):
    if role == "admin":
        from awgbot.bot.handlers.admin import panel, clients, devices
        if kind == "main":
            return await panel._panel_parts(services)
        if kind in ("rf", "sites"):
            from awgbot.bot.handlers import routing as rt
            return await rt.screen_for(services, None, ref, kind)
        if kind == "cl":
            return await clients.client_card_parts(services, ref)
        if kind == "edit":
            return await clients.client_edit_parts(services, ref)
        if kind == "clients":
            return await clients.clients_screen(services, chat_id)
        if kind == "extend":
            return await clients.extend_screen(services, ref)
        if kind == "dev":
            dev = services.db.get_device(ref)
            return None if dev is None else await devices.device_card_parts(services, dev)
        if kind == "devices":
            return await devices.my_devices_parts(services, chat_id)
        if kind == "online":
            return await panel.online_screen(services)
        if kind == "expiring":
            return await panel.expiring_screen(services)
        if kind == "unassigned":
            return await panel.unassigned_screen(services)
        if kind == "traffic":
            return await panel.traffic_profiles_screen(services)
        if kind == "traffic_dev":
            return await panel.traffic_devices_screen(services, ref)
        if kind == "gw":
            return await panel.gateway_card_screen(services, ref, chat_id)
        if kind == "upd":
            from awgbot.bot.handlers.settings import _screen
            return await _screen("upd", services)
        return None
    if role == "client":
        from awgbot.bot.handlers import client as ch
        if kind == "main":
            return await ch.main_payload(services, client)
        if kind == "devices":
            return await ch.devices_payload(services, client, chat_id)
        if kind == "dev":
            from awgbot.bot.handlers.common import mine_or_held
            dev = mine_or_held(services, client, ref)
            return None if dev is None else await ch.device_card_parts(services, client, dev)
        if kind == "sub":
            return await ch.sub_parts(services, client.id)
        if kind in ("rf", "sites"):
            from awgbot.bot.handlers import routing as rt
            return await rt.screen_for(services, client, client.id, kind)
        if kind == "guide":
            from awgbot.bot.handlers import guide as gh
            return await gh.connect_step0_payload(services, client, ref, chat_id)
        return None
    if role == "invited":
        from awgbot.bot.handlers import friend as fh
        if kind == "main":
            return await fh.guest_main_payload(services, client)
        if kind == "devices":
            return await fh.devices_payload(services, client, chat_id)
        if kind == "dev":
            dev = await fh.held_one(services, client, ref)
            return None if dev is None else await fh.card_payload(services, dev)
        if kind in ("rf", "sites"):
            from awgbot.bot.handlers import routing as rt
            return await rt.screen_for(services, client, client.id, kind)
        if kind == "guide":
            from awgbot.bot.handlers import guide as gh
            return await gh.connect_step0_payload(services, client, ref, chat_id)
        return None
    return None


__all__ = ["render", "with_note"]
