"""
handlers/admin/clients/ — профили глазами администратора.
Части: card — роутер, список, карточка, подменю «✏️ Изменить»; notify —
тумблер «уведомить клиента» на экранах правки условий; create — новый
профиль пресетами с приглашением; edit — имя, лимиты, период вручную; delete —
новое приглашение и удаление; extend — продление с тумблерами остатка и
уведомления, снятие паузы. Роутер один (в card), части регистрируют на нём и
импортируются в порядке прежнего модуля — порядок обработчиков не изменился.

Список, карточка, подменю «✏️ Изменить», создание пресетами с приглашением,
имя / лимиты / период с итогом первой строкой, продление с тумблером остатка,
удаление, новое приглашение, снятие паузы. Ввод текста — на месте экрана
(ask_here), возврат — в экран-контекст (back_to_context).
"""
from __future__ import annotations

from .card import (
    router, clients_screen, clients_list, client_card_parts, client_edit_parts, _show_client_card,
    client_open, client_edit)
from .notify import notify_toggle, notify_on, reset_notify, client_notes
from .create import (
    add_client_start, add_client_name, add_client_devs_preset, _devs_chosen, add_client_limit,
    add_client_traffic_preset, _traffic_chosen, add_client_traffic, add_client_period, _invite_menu)
from .edit import (
    _client_or_alert, edit_name_start, edit_name_apply, edit_limit_start, _apply_devs_limit,
    edit_limit_preset, edit_limit_apply, edit_client_traffic_start, _apply_traffic_limit,
    edit_traffic_preset, edit_traffic_apply, edit_period_start, edit_period_start_apply,
    edit_period_end_apply, edit_period_button)
from .delete import regen_invite, client_delete_confirm, client_delete_apply
from .extend import (
    extend_screen, extend_start, extend_keep_toggle, extend_period_chosen, _do_extend,
    admin_resume_pause, admin_resume_pause_yes)

__all__ = ["router", "clients_screen", "clients_list", "client_card_parts", "client_edit_parts",
    "_show_client_card", "client_open", "client_edit", "add_client_start", "add_client_name",
    "add_client_devs_preset", "_devs_chosen", "add_client_limit", "add_client_traffic_preset",
    "_traffic_chosen", "add_client_traffic", "add_client_period", "_invite_menu",
    "_client_or_alert", "edit_name_start", "edit_name_apply", "edit_limit_start",
    "_apply_devs_limit", "edit_limit_preset", "edit_limit_apply", "edit_client_traffic_start",
    "_apply_traffic_limit", "edit_traffic_preset", "edit_traffic_apply", "edit_period_start",
    "edit_period_start_apply", "edit_period_end_apply", "edit_period_button", "regen_invite",
    "client_delete_confirm", "client_delete_apply", "extend_screen", "extend_start", "extend_keep_toggle",
    "extend_period_chosen", "_do_extend", "admin_resume_pause", "admin_resume_pause_yes",
    "notify_toggle", "notify_on", "reset_notify", "client_notes"]
