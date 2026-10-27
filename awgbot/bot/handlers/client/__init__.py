"""
handlers/client/ — роутер пользователя (и активация инвайта).
Части: main — роутер, главная, /start и активация (в том числе кодом друга и
кодом у действующего профиля); menu — меню, «💳 Подписка», выдача, карточка
устройства и её действия (имя, лимит, передача другу); add — добавление
устройства (себе и другу) и удаление; pause — помощь и отсрочка, ручная
блокировка своего устройства, пауза подписки. Роутер один (в main), части
регистрируют на нём и импортируются в порядке прежнего модуля — порядок
обработчиков не изменился.

Тонкие обработчики: приняли → проверили владение → позвали services → отрисовали.
Тяжёлые вызовы идут через common.call (to_thread). Статус сервера в приветствии —
из кэша монитора (ни одного exec на /start).

Ввод текста — на месте экрана (ask_here) с инлайн «✖️ Отмена»; после ввода
бот рисует экран-контекст заново с итогом первой строкой (back_to_context).
Числа — пресетами (PresetCB), «✏️ Другое» — ввод.
"""
from __future__ import annotations

from .main import (
    router, ADD_FROM_DEVICES, _bot_username, main_payload, _show_main, start_activation, start_cold,
    code_activation, _try_activate, _activate_friend, _notify_owner_activated, take_code_as_member,
    parse_link, start_client_with_code, open_list_link, code_client, start_client)
from .menu import (
    menu_main, _pause_flags, sub_parts, menu_info, devices_payload, menu_devices, _issuable,
    menu_gen_pick, _issue, device_card_parts, device_open, client_device_edit_name_start,
    client_device_edit_name_apply, device_connect_menu, client_edit_device_traffic,
    _apply_device_limit, device_limit_preset, client_edit_traffic_apply, device_transfer_ask,
    _send_invite, device_transfer_do, device_reinvite, device_gen)
from .add import (
    device_add_start, device_add_for_whom, device_add_for_whom_stale, device_add_name, _limit_note,
    _create_own, _create_for_friend, device_add_limit_preset, device_add_traffic,
    _show_delete_prompt, device_delete_ask, device_delete_confirm)
from .pause import (
    help_root, help_skip, grace_take, _blockable, client_block_ask, client_block_device,
    client_unblock_device, _show_sub, pause_ask, _enter_pause, pause_pick, pause_other,
    pause_other_apply, _user_pause_guard, pause_resume, pause_cancel)

__all__ = ["router", "ADD_FROM_DEVICES", "_bot_username", "main_payload", "_show_main", "start_activation",
    "start_cold", "code_activation", "_try_activate", "_activate_friend", "_notify_owner_activated",
    "take_code_as_member", "parse_link", "start_client_with_code", "open_list_link", "code_client", "start_client",
    "menu_main", "_pause_flags", "sub_parts", "menu_info", "devices_payload", "menu_devices",
    "_issuable", "menu_gen_pick", "_issue", "device_card_parts", "device_open",
    "client_device_edit_name_start", "client_device_edit_name_apply", "device_connect_menu",
    "client_edit_device_traffic", "_apply_device_limit", "device_limit_preset",
    "client_edit_traffic_apply", "device_transfer_ask", "_send_invite", "device_transfer_do",
    "device_reinvite", "device_gen", "device_add_start", "device_add_for_whom",
    "device_add_for_whom_stale", "device_add_name", "_limit_note", "_create_own",
    "_create_for_friend", "device_add_limit_preset", "device_add_traffic", "_show_delete_prompt",
    "device_delete_ask", "device_delete_confirm", "help_root", "help_skip", "grace_take",
    "_blockable", "client_block_ask", "client_block_device", "client_unblock_device", "_show_sub",
    "pause_ask", "_enter_pause", "pause_pick", "pause_other", "pause_other_apply",
    "_user_pause_guard", "pause_resume", "pause_cancel"]
