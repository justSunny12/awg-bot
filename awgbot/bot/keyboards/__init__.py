"""
keyboards — инлайн-клавиатуры (aiogram). Callback-data берутся из callbacks.py.

Пакет разрезан по экранам:

  common     reply-клавиатура, маркеры состояния, период, да/нет, блокировки
  client     меню клиента и гостя, карточка устройства, выдача, гайды, пауза
  admin      меню администратора, профили, устройства без клиента, списки
  routing    условная маршрутизация и шлюз (сторона основного бота)
  settings   экран «⚙️ Настройки», обновления, переезд
  broadcast  объявления пользователям
  gateway    роль gateway (агент на шлюзе)
"""

from __future__ import annotations

from .common import (
    BTN_CANCEL, reply_cancel, reply_hide, period_choices, yes_no, to_menu,
    append_hide_row, hide_only, block_pause_choice, block_notify_choice,
    block_unblock_reasons)
from .client import (
    client_main, client_devices, held_device_actions, lent_out_device_actions,
    block_device_confirm, guest_main, guest_devices, device_actions,
    connect_method_choice, connect_method_choice_friend, PICK_DEVICE_PROMPT,
    GEN_ACTIONS, gen_kind, pick_device, issuable, confirm_transfer, add_for_whom, help_menu,
    friend_finisher, guest_pick_device, friend_help_back, friend_help_menu,
    confirm_delete_device, pick_device_to_delete, added_by_admin,
    unmanaged_device_dialog, guide_nav, guide_connect_method, guide_connect_done,
    guide_connect_devices, grace_offer, client_info_actions, pause_day_choice,
    pause_confirm, pause_resume_confirm)
from .admin import (
    admin_add_device_choice, pick_client_for_add_device, admin_main, admin_clients,
    admin_client_actions, admin_client_back, admin_client_device_list,
    unassigned_devices, reassign_targets, reassign_addslot, confirm_lower_limit,
    traffic_profiles_kb, expiring_kb, online_devices_kb, traffic_devices_kb)
from .settings import (
    settings_root, settings_back, settings_server, private_dns_choices,
    private_dns_offer_kb, migration_prepare_confirm, migration_generation_pending,
    settings_firewall, settings_notify, CLIENT_EVENT_LABELS, settings_notify_clients,
    settings_email, email_forget_confirm, settings_subs, settings_mon, settings_backup,
    backup_encryption_kb, restore_confirm, email_setup_offer, settings_svc,
    svc_confirm, migration_confirm, settings_updates, settings_cancel, update_notify,
    update_admin_available, migration_needed, update_done_menu)
from .routing import (
    routing_devices, routing_panel, routing_clear_confirm, gateway_device_actions,
    gateway_choose_kind, gateway_pick, gateway_mark_confirm, gateway_new_confirm,
    gateway_remove_confirm, routing_disable_confirm, settings_routing,
    routing_provision, settings_routing_bundle, settings_routing_lists,
    settings_routing_users, bundle_menu_kb, gateway_list, gateway_card,
    gateway_switch_confirm, gateway_slot_cancel, settings_routing_monitor,
    gateway_lan_confirm, gateway_router_back, gateway_peer_confirm)
from .broadcast import (
    broadcast_mode, broadcast_targets, broadcast_cancel, broadcast_confirm)
from .gateway import (
    gateway_panel_kb, gateway_settings_kb, gateway_notify_kb, gateway_mon_kb,
    gateway_backup_kb, gateway_email_kb, gateway_email_forget_confirm,
    gateway_email_offer, gateway_encryption_kb, gateway_cancel_kb, gateway_maint_kb,
    gateway_updates_kb, gateway_confirm_kb, gateway_bundle_kb,
    gateway_bundle_passphrase_kb, gateway_back_kb, gateway_update_available_kb,
    gateway_lan_kb, gateway_lan_list_kb)

__all__ = [
    "BTN_CANCEL", "reply_cancel", "reply_hide", "period_choices", "yes_no", "to_menu",
    "append_hide_row", "hide_only", "block_pause_choice", "block_notify_choice",
    "block_unblock_reasons", "client_main", "client_devices", "held_device_actions",
    "lent_out_device_actions", "block_device_confirm", "guest_main", "guest_devices",
    "device_actions", "connect_method_choice", "connect_method_choice_friend",
    "PICK_DEVICE_PROMPT", "GEN_ACTIONS", "gen_kind", "pick_device", "issuable", "confirm_transfer",
    "add_for_whom", "help_menu", "friend_finisher", "guest_pick_device",
    "friend_help_back", "friend_help_menu", "confirm_delete_device",
    "pick_device_to_delete", "added_by_admin", "unmanaged_device_dialog", "guide_nav",
    "guide_connect_method", "guide_connect_done", "guide_connect_devices",
    "grace_offer", "client_info_actions", "pause_day_choice", "pause_confirm",
    "pause_resume_confirm", "admin_add_device_choice", "pick_client_for_add_device",
    "admin_main", "admin_clients", "admin_client_actions", "admin_client_back",
    "admin_client_device_list", "unassigned_devices", "reassign_targets",
    "reassign_addslot", "confirm_lower_limit", "traffic_profiles_kb", "expiring_kb",
    "online_devices_kb", "traffic_devices_kb", "settings_root", "settings_back",
    "settings_server", "private_dns_choices", "private_dns_offer_kb",
    "migration_prepare_confirm", "migration_generation_pending", "settings_firewall",
    "settings_notify", "CLIENT_EVENT_LABELS", "settings_notify_clients",
    "settings_email", "email_forget_confirm", "settings_subs", "settings_mon",
    "settings_backup", "backup_encryption_kb", "restore_confirm", "email_setup_offer",
    "settings_svc", "svc_confirm", "migration_confirm", "settings_updates",
    "settings_cancel", "update_notify", "update_admin_available", "migration_needed",
    "update_done_menu", "routing_devices", "routing_panel", "routing_clear_confirm",
    "gateway_device_actions", "gateway_choose_kind", "gateway_pick",
    "gateway_mark_confirm", "gateway_new_confirm", "gateway_remove_confirm",
    "routing_disable_confirm", "settings_routing", "routing_provision",
    "settings_routing_bundle", "settings_routing_lists", "settings_routing_users",
    "bundle_menu_kb", "gateway_list", "gateway_card", "gateway_switch_confirm", "gateway_lan_confirm", "gateway_router_back", "gateway_peer_confirm",
    "gateway_slot_cancel", "settings_routing_monitor", "broadcast_mode", "broadcast_targets", "broadcast_cancel",
    "broadcast_confirm", "gateway_panel_kb", "gateway_settings_kb",
    "gateway_notify_kb", "gateway_mon_kb", "gateway_backup_kb", "gateway_email_kb",
    "gateway_email_forget_confirm", "gateway_email_offer", "gateway_encryption_kb",
    "gateway_cancel_kb", "gateway_maint_kb", "gateway_updates_kb",
    "gateway_confirm_kb", "gateway_bundle_kb", "gateway_bundle_passphrase_kb",
    "gateway_back_kb", "gateway_update_available_kb", "gateway_lan_kb", "gateway_lan_list_kb",
]
