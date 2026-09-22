"""
texts — шаблоны сообщений (русский, с эмодзи) и форматтеры отображения.

Вынесено из логики, чтобы UI правился без копания в services/handlers. Пакет
разрезан по экранам:

  fmt        экранирование, объёмы, устройства, ссылки на людей, склонения
  client     главный экран клиента и гостя, подписка, пауза, отсрочка, устройства
  admin      панель администратора, списки, создание и продление профилей
  routing    условная маршрутизация и шлюз (сторона основного бота)
  migration  переезд профилей
  updates    обновления бота (self-update)
  settings   экран настроек, почта, резервные копии, файервол
  broadcast  объявления пользователям
  gateway    роль gateway (агент на шлюзе)
  common     статус awg-сервиса, отмена диалога, перезапуск

Направление трафика (важно не перепутать): в awg dump rx = принято сервером
ОТ клиента = аплоад клиента; tx = отдано клиенту = даунлоад клиента. В БД
traffic_rx_* = аплоад, traffic_tx_* = даунлоад. В карточке показываем
↓ скачано = tx, ↑ загружено = rx.
"""

from __future__ import annotations

from .fmt import (
    _e, gb, human_bytes, used_of_limit, gb_str, device_created_report,
    consumption_line, consumption_line_admin, client_total_line, device_label,
    plain_ip, device_line, device_card_text, tg_link, client_link, owner_name,
    holder_name, owner_link, holder_link, client_label, _n_devices, plural_ru,
    device_emoji)
from .migration import (
    migration_panel_line, migration_profile_line, migration_pending_text,
    migration_start_confirm, migration_started, migration_finish_confirm,
    migration_cancel_confirm, migration_hello, migration_ready, migration_orphans_text,
    migration_cancelled, migration_finished, migration_prepare_intro,
    MIGRATION_ASK_PORT, migration_prepared, migration_prepare_failed,
    migration_generation_pending, migration_needed, migration_promoted,
    migration_promote_restart_failed)
from .routing import (
    gateway_device_card, gateway_claim_marked, gateway_claim_already, ROUTING_NAME,
    SETTINGS_ROUTING_ABSENT, routing_lists_block, ROUTING_BUNDLE_INTRO,
    channel_block, channel_drift_block,
    settings_routing_text, settings_routing_gateway_line, GATEWAY_CHOOSE_INTRO,
    GATEWAY_PICK_INTRO, GATEWAY_PICK_EMPTY, gateway_new_ask, gateway_mark_ask,
    gateway_marked, gateway_remove_ask, gateway_removed, ROUTING_PROVISION_INTRO,
    routing_provisioned, routing_provision_failed, gateway_ask_token, GW_INSTALL_URL,
    ping_fmt, ping_line, ext_ip_line, vps_hostname, gateway_role_line, slot_name, slot_short, slot_status,
    settings_routing_gateway_block, gateway_list_text, gateway_card_text,
    GATEWAY_STANDBY_CHOOSE_INTRO, gateway_replace_intro, gateway_switch_ask,
    gateway_home_text, gateway_home_report, gateway_label_text, routing_monitor_text,
    gateway_install_instructions, gateway_lan_ask, gateway_peer_ask, peer_nets_line, GATEWAY_LAN_NO_SUBNET, gateway_router_text, ROUTER_IP_PLACEHOLDER, SETTINGS_ROUTING_SUBOFF, routing_lists_text,
    routing_users_text, routing_status_line, routing_admin_status_line, ROUTING_ABOUT, ROUTING_ABOUT_OFF,
    ROUTING_ADD_PROMPT, ROUTING_APPLY_HINT, ROUTING_ADDED_HINT, routing_domain_removed,
    routing_panel_text, routing_add_report, ROUTING_CLEAR_CONFIRM, ROUTING_UNAVAILABLE,
    routing_gateway_warning, ROUTING_DISABLE_CONFIRM, ROUTING_GRANTED_NOTICE,
    routing_granted_holder_notice, routing_revoked_holder_notice,
    ROUTING_REVOKED_NOTICE, ROUTING_LENT_OUT_NOTE, routing_devices_text)
from .updates import (
    update_available, update_current_ok, update_admin_available, update_blocked,
    update_wait, update_failed, update_applied, update_not_applied)
from .client import (
    subscription_block, greeting_guest, held_devices_tail, held_device_card,
    lent_out_marker, device_delete_by_holder_ask, device_delete_by_owner_ask,
    lent_device_deleted_by_holder_notice, lent_device_deleted_by_admin_notice,
    lent_device_reassigned_notice, lent_device_deleted_by_owner_notice,
    friend_device_added, friend_other_donor_refusal, guest_upgraded,
    guest_upgraded_donor_notice, guest_upgraded_admin_tail, GUEST_NO_DEVICES_LEFT,
    block_device_ask, FRIEND_ALREADY_USER, friend_activated,
    friend_activated_host_notice, finish_link, finish_qr, finish_file, finish_config,
    CONNECT_METHOD_ASK, FINISH_CLIENT_INVITE, FINISH_FRIEND_INVITE, ADD_FOR_WHOM,
    friend_invite_message, friend_marker, TRANSFER_FRIEND_WARNING, client_card,
    subscription_kind_label, pause_credit_line, pause_credit_admin, pause_balance_line,
    subscription_manage_text, subscription_status_only, server_status_client,
    greeting_client, device_deleted, device_slots_line, DELETE_ONLY_DEVICE_WARNING,
    DELETE_DEVICE_CONFIRM, HELP_INTRO, UNMANAGED_DEVICE_EXPLAIN,
    UNMANAGED_DEVICE_DIALOG, limit_changed_notice, INVITE_FORWARD_TEMPLATE,
    ACTIVATION_OK, ACTIVATION_INVALID, ACTIVATION_ALREADY, COLD_START_GREETING,
    CODE_NO_ARG, grace_activated_client, GRACE_STALE, grace_activated_admin, pause_ask,
    pause_warning, pause_emergency_code, pause_entered_summary, pause_unavailable,
    pause_limit_exhausted, pause_resume_ask, pause_resumed_self)
from .admin import (
    traffic_profiles_text, online_devices_text, traffic_devices_text, expiring_text,
    admin_panel, CLIENT_DELETE_PARTIAL, admin_bootstrap_device, reassign_donor_notice,
    reassign_recipient_notice, reassign_recipient_notice_with_slot,
    activated_admin_notice, client_created_report, LIMIT_REACHED, EXTEND_KEEP_QUESTION,
    TRAFFIC_LIMIT_CLIENT_ASK, traffic_limit_device_ask, TRAFFIC_LIMIT_BAD)
from .settings import (
    SETTINGS_ROOT, SETTINGS_NOTIFY_CLIENTS, SETTINGS_NOTIFY, SETTINGS_SUBS,
    settings_email_text, EMAIL_ASK_ADDRESS, EMAIL_ASK_IMAP_HOST, EMAIL_ASK_IMAP_PORT,
    EMAIL_ASK_SMTP_HOST, EMAIL_ASK_SMTP_PORT, EMAIL_BAD_ADDRESS, EMAIL_BAD_PORT,
    EMAIL_BAD_HOST, EMAIL_FORGET_CONFIRM, EMAIL_FORGOTTEN, email_test_sent,
    email_ask_address_change, email_ask_resume_address, email_provider_line,
    email_ask_password, email_saved, email_check_failed, SETTINGS_MON, SETTINGS_BACKUP,
    restore_offer, restore_rejected, RESTORE_STARTED, restore_done,
    EMAIL_NOT_CONFIGURED, BACKUP_NEEDS_ENCRYPTION, backup_encryption_text,
    BACKUP_ASK_PASSPHRASE, BACKUP_ASK_PASSPHRASE_AGAIN, BACKUP_PASSPHRASE_MISMATCH,
    BACKUP_PASSPHRASE_SET, backup_mailed, SETTINGS_SVC, SVC_CONFIRM_AWG,
    SVC_CONFIRM_BOT, settings_svc_text, SETTINGS_UPD, settings_upd_text,
    SETTINGS_BOUNDS, SETTINGS_TEXT, PRIVATE_DNS_WHAT, private_dns_offer,
    PRIVATE_DNS_LATER, PRIVATE_DNS_DISMISSED, settings_server_text,
    settings_firewall_text, firewall_confirmed, firewall_rolled_back,
    SSH_PORT_ASK, ssh_port_busy, ssh_port_same, ssh_port_changed, ssh_owner_refusal,
    settings_prompt, settings_changed, settings_ssh_allow_added, settings_bad_value)
from .broadcast import (
    BROADCAST_EMPTY, BROADCAST_MODE, BROADCAST_TARGETS, BROADCAST_TARGETS_EXTEND,
    BROADCAST_NO_TARGETS, BROADCAST_ALL_UNLIMITED, BROADCAST_DAYS_BAD,
    subscription_mark, broadcast_days_prompt, extension_header, announcement_text,
    extension_reserve, broadcast_prompt, broadcast_preview, broadcast_preview_photos,
    broadcast_too_many_photos, broadcast_too_long, broadcast_report)
from .gateway import (
    gateway_panel, gateway_health, gateway_lan_text, gateway_lan_ask_domain, gateway_lan_own_text,
    gateway_lan_result, GW_SETTINGS, GW_SETTINGS_NOTIFY, GW_SETTINGS_MON,
    GW_BACKUP_NO_KEY, host_rebooted, GW_MAINT, GW_CONFIRM_RESTART, GW_CONFIRM_REASSERT,
    GW_CONFIRM_BOT_RESTART, GW_BOT_RESTARTING, GW_BUNDLE_NOT_OURS,
    GW_BUNDLE_PASSPHRASE_QUESTION, gateway_claim_forward_text, gateway_apply_report,
    gateway_op_result, awg_restart_warning_body, gateway_bundle_received,
    gateway_ssh_text, GW_SSH_PORT_ASK, gateway_ssh_owner_refusal, gateway_ssh_port_changed,
    GW_SSH_ALLOW_ASK, gateway_ssh_allow_added, GW_SSH_ALLOW_ALREADY, gateway_ssh_filter_on_ask,
    GW_SSH_FILTER_OFF, GW_SSH_FILTER_OFF_ASK, gateway_ssh_del_ask, gateway_ssh_panel_line)
from .common import HB_SERVER_DOWN, HB_SERVER_UP, cancelled, BOT_RESTARTED

__all__ = [
    "_e", "gb", "human_bytes", "used_of_limit", "gb_str", "device_created_report",
    "consumption_line", "consumption_line_admin", "client_total_line", "device_label",
    "plain_ip", "device_line", "device_card_text", "tg_link", "client_link",
    "owner_name", "holder_name", "owner_link", "holder_link", "client_label",
    "_n_devices", "plural_ru", "device_emoji", "migration_panel_line",
    "migration_profile_line", "migration_pending_text", "migration_start_confirm",
    "migration_started", "migration_finish_confirm", "migration_cancel_confirm",
    "migration_hello", "migration_ready", "migration_orphans_text",
    "migration_cancelled", "migration_finished", "migration_prepare_intro",
    "MIGRATION_ASK_PORT", "migration_prepared", "migration_prepare_failed",
    "migration_generation_pending", "migration_needed", "migration_promoted",
    "migration_promote_restart_failed", "gateway_device_card", "gateway_claim_marked",
    "gateway_claim_already", "ROUTING_NAME", "SETTINGS_ROUTING_ABSENT",
    "routing_lists_block", "ROUTING_BUNDLE_INTRO", "channel_block", "channel_drift_block", "settings_routing_text",
    "settings_routing_gateway_line", "GATEWAY_CHOOSE_INTRO", "GATEWAY_PICK_INTRO",
    "GATEWAY_PICK_EMPTY", "gateway_new_ask", "gateway_mark_ask", "gateway_marked",
    "gateway_remove_ask", "gateway_removed", "ROUTING_PROVISION_INTRO",
    "routing_provisioned", "routing_provision_failed", "gateway_ask_token",
    "ping_fmt", "ping_line", "ext_ip_line", "vps_hostname", "gateway_role_line", "slot_name",
    "slot_short", "slot_status",
    "settings_routing_gateway_block", "gateway_list_text", "gateway_card_text",
    "GATEWAY_STANDBY_CHOOSE_INTRO", "gateway_replace_intro", "gateway_switch_ask",
    "gateway_home_text", "gateway_home_report", "gateway_label_text", "routing_monitor_text",
    "GW_INSTALL_URL", "gateway_install_instructions", "gateway_lan_ask", "gateway_peer_ask", "peer_nets_line", "GATEWAY_LAN_NO_SUBNET", "gateway_router_text", "ROUTER_IP_PLACEHOLDER", "SETTINGS_ROUTING_SUBOFF",
    "routing_lists_text", "routing_users_text", "routing_status_line", "routing_admin_status_line", "ROUTING_ABOUT",
    "ROUTING_ABOUT_OFF", "ROUTING_ADD_PROMPT", "ROUTING_APPLY_HINT",
    "ROUTING_ADDED_HINT", "routing_domain_removed", "routing_panel_text",
    "routing_add_report", "ROUTING_CLEAR_CONFIRM", "ROUTING_UNAVAILABLE",
    "routing_gateway_warning", "ROUTING_DISABLE_CONFIRM", "ROUTING_GRANTED_NOTICE",
    "routing_granted_holder_notice", "routing_revoked_holder_notice",
    "ROUTING_REVOKED_NOTICE", "ROUTING_LENT_OUT_NOTE", "routing_devices_text",
    "update_available", "update_current_ok", "update_admin_available",
    "update_blocked", "update_wait", "update_failed", "update_applied",
    "update_not_applied", "subscription_block", "greeting_guest", "held_devices_tail",
    "held_device_card", "lent_out_marker", "device_delete_by_holder_ask",
    "device_delete_by_owner_ask", "lent_device_deleted_by_holder_notice",
    "lent_device_deleted_by_admin_notice", "lent_device_reassigned_notice",
    "lent_device_deleted_by_owner_notice", "friend_device_added",
    "friend_other_donor_refusal", "guest_upgraded", "guest_upgraded_donor_notice",
    "guest_upgraded_admin_tail", "GUEST_NO_DEVICES_LEFT", "block_device_ask",
    "FRIEND_ALREADY_USER", "friend_activated", "friend_activated_host_notice",
    "finish_link", "finish_qr", "finish_file", "finish_config", "CONNECT_METHOD_ASK",
    "FINISH_CLIENT_INVITE", "FINISH_FRIEND_INVITE", "ADD_FOR_WHOM",
    "friend_invite_message", "friend_marker", "TRANSFER_FRIEND_WARNING", "client_card",
    "subscription_kind_label", "pause_credit_line", "pause_credit_admin",
    "pause_balance_line", "subscription_manage_text", "subscription_status_only",
    "server_status_client", "greeting_client", "device_deleted", "device_slots_line",
    "DELETE_ONLY_DEVICE_WARNING", "DELETE_DEVICE_CONFIRM", "HELP_INTRO",
    "UNMANAGED_DEVICE_EXPLAIN", "UNMANAGED_DEVICE_DIALOG", "limit_changed_notice",
    "INVITE_FORWARD_TEMPLATE", "ACTIVATION_OK", "ACTIVATION_INVALID",
    "ACTIVATION_ALREADY", "COLD_START_GREETING", "CODE_NO_ARG",
    "grace_activated_client", "GRACE_STALE", "grace_activated_admin", "pause_ask",
    "pause_warning", "pause_emergency_code", "pause_entered_summary",
    "pause_unavailable", "pause_limit_exhausted", "pause_resume_ask",
    "pause_resumed_self", "traffic_profiles_text", "online_devices_text",
    "traffic_devices_text", "expiring_text", "admin_panel", "CLIENT_DELETE_PARTIAL",
    "admin_bootstrap_device", "reassign_donor_notice", "reassign_recipient_notice",
    "reassign_recipient_notice_with_slot", "activated_admin_notice",
    "client_created_report", "LIMIT_REACHED", "EXTEND_KEEP_QUESTION",
    "TRAFFIC_LIMIT_CLIENT_ASK", "traffic_limit_device_ask", "TRAFFIC_LIMIT_BAD",
    "SETTINGS_ROOT", "SETTINGS_NOTIFY_CLIENTS", "SETTINGS_NOTIFY", "SETTINGS_SUBS",
    "settings_email_text", "EMAIL_ASK_ADDRESS", "EMAIL_ASK_IMAP_HOST",
    "EMAIL_ASK_IMAP_PORT", "EMAIL_ASK_SMTP_HOST", "EMAIL_ASK_SMTP_PORT",
    "EMAIL_BAD_ADDRESS", "EMAIL_BAD_PORT", "EMAIL_BAD_HOST", "EMAIL_FORGET_CONFIRM",
    "EMAIL_FORGOTTEN", "email_test_sent", "email_ask_address_change",
    "email_ask_resume_address", "email_provider_line", "email_ask_password",
    "email_saved", "email_check_failed", "SETTINGS_MON", "SETTINGS_BACKUP",
    "restore_offer", "restore_rejected", "RESTORE_STARTED", "restore_done",
    "EMAIL_NOT_CONFIGURED", "BACKUP_NEEDS_ENCRYPTION", "backup_encryption_text",
    "BACKUP_ASK_PASSPHRASE", "BACKUP_ASK_PASSPHRASE_AGAIN",
    "BACKUP_PASSPHRASE_MISMATCH", "BACKUP_PASSPHRASE_SET", "backup_mailed",
    "SETTINGS_SVC", "SVC_CONFIRM_AWG", "SVC_CONFIRM_BOT", "settings_svc_text",
    "SETTINGS_UPD", "settings_upd_text", "SETTINGS_BOUNDS", "SETTINGS_TEXT",
    "PRIVATE_DNS_WHAT", "private_dns_offer", "PRIVATE_DNS_LATER",
    "PRIVATE_DNS_DISMISSED", "settings_server_text", "settings_firewall_text",
    "firewall_confirmed", "firewall_rolled_back", "settings_prompt",
    "SSH_PORT_ASK", "ssh_port_busy", "ssh_port_same", "ssh_port_changed", "ssh_owner_refusal",
    "settings_changed", "settings_ssh_allow_added", "settings_bad_value",
    "BROADCAST_EMPTY", "BROADCAST_MODE", "BROADCAST_TARGETS",
    "BROADCAST_TARGETS_EXTEND", "BROADCAST_NO_TARGETS", "BROADCAST_ALL_UNLIMITED",
    "BROADCAST_DAYS_BAD", "subscription_mark", "broadcast_days_prompt",
    "extension_header", "announcement_text", "extension_reserve", "broadcast_prompt",
    "broadcast_preview", "broadcast_preview_photos", "broadcast_too_many_photos",
    "broadcast_too_long", "broadcast_report", "gateway_panel", "gateway_health",
    "gateway_lan_text", "gateway_lan_ask_domain", "gateway_lan_own_text", "gateway_lan_result",
    "GW_SETTINGS", "GW_SETTINGS_NOTIFY", "GW_SETTINGS_MON", "GW_BACKUP_NO_KEY",
    "host_rebooted", "GW_MAINT", "GW_CONFIRM_RESTART", "GW_CONFIRM_REASSERT",
    "GW_CONFIRM_BOT_RESTART", "GW_BOT_RESTARTING", "GW_BUNDLE_NOT_OURS",
    "GW_BUNDLE_PASSPHRASE_QUESTION", "gateway_claim_forward_text",
    "gateway_ssh_text", "GW_SSH_PORT_ASK", "gateway_ssh_owner_refusal", "gateway_ssh_port_changed",
    "GW_SSH_ALLOW_ASK", "gateway_ssh_allow_added", "GW_SSH_ALLOW_ALREADY", "gateway_ssh_filter_on_ask",
    "GW_SSH_FILTER_OFF", "GW_SSH_FILTER_OFF_ASK", "gateway_ssh_del_ask",
    "gateway_ssh_panel_line",
    "gateway_apply_report", "gateway_op_result", "awg_restart_warning_body",
    "gateway_bundle_received", "HB_SERVER_DOWN", "HB_SERVER_UP", "cancelled",
    "BOT_RESTARTED",
]
