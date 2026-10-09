"""
texts — шаблоны сообщений (русский, с эмодзи) и форматтеры отображения.

Вынесено из логики, чтобы UI правился без копания в services/handlers. Пакет
разрезан по экранам:

  fmt        экранирование, ссылки на экраны, дерево, объёмы, устройства,
             ссылки на людей, склонения
  client     главный экран клиента и гостя, подписка, пауза, отсрочка, устройства
  admin      панель администратора, списки, трафик деревом, карточки,
             создание и продление профилей
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
    _e, gb, human_bytes, used_of_limit, gb_str, device_state, details, deep_link,
    profile_link, admin_device_link, tree, sub_line, rf_value, updown_brief, access_status_line,
    client_total_line, device_label,
    plain_ip, tg_link, client_link, owner_name,
    holder_name, owner_link, holder_link, _n_devices, plural_ru,
    device_emoji)
from .migration import (
    migration_pending_text,
    migration_start_confirm, migration_started, migration_finish_confirm,
    migration_cancel_confirm, migration_hello, migration_ready, migration_orphans_text,
    migration_cancelled, migration_finished, migration_prepare_intro,
    MIGRATION_ASK_PORT, migration_prepared, migration_prepare_failed,
    migration_generation_pending, migration_needed, migration_promoted)
from awgbot.domain.services.gwchannel import drift_lines   # строки расхождения рисует домен
from .routing import (
    gateway_device_card, gateway_claim_marked, gateway_claim_already, ROUTING_NAME,
    SETTINGS_ROUTING_ABSENT, GATEWAYS_OFF, GATEWAYS_ABOUT, GATEWAYS_AUTO_OFF,
    channel_lines, GW_CARD_PAYLOAD, agent_bot_line, gateway_bundle_caption,
    gateway_plain_bundle_caption, gateway_installed_text, gateway_bundle_applied_text,
    GATEWAY_CHOOSE_INTRO, GATEWAY_PICK_INTRO, GATEWAY_PICK_EMPTY, gateway_new_ask, gateway_mark_ask,
    gateway_remove_ask, gateway_removed, ROUTING_PROVISION_INTRO,
    routing_provisioned, routing_provision_failed, gateway_ask_token, GW_INSTALL_URL,
    ping_fmt, ping_line, ext_ip_line, vps_hostname, gateway_role_line, slot_name, slot_short, slot_status,
    slot_line, gateways_text, gateway_card_text, gateway_edit_text,
    GATEWAY_STANDBY_CHOOSE_INTRO, gateway_replace_intro, gateway_switch_ask,
    gateway_home_text, gateway_home_report, gateway_label_text, routing_params_text,
    gateway_install_instructions, gateway_lan_ask, already_state, GW_TOKEN_NOT_FORGOTTEN, gateway_peer_ask, peer_nets_line, GATEWAY_LAN_NO_SUBNET,
    gateway_router_text, ROUTER_IP_PLACEHOLDER, ROUTER_TABS, SETTINGS_ROUTING_SUBOFF,
    routing_users_text, routing_status_line, routing_admin_status_line, ROUTING_ABOUT, ROUTING_ABOUT_OFF,
    ROUTING_ADD_PROMPT, ROUTING_APPLY_HINT, ROUTING_ADDED_HINT, routing_domain_removed,
    routing_panel_text, routing_sites_text, routing_add_report, routing_clear_ask, ROUTING_CLEAR_CONFIRM, ROUTING_UNAVAILABLE, ROUTING_NOT_ALLOWED_ADMIN,
    routing_gateway_warning, ROUTING_DISABLE_CONFIRM, ROUTING_GRANTED_NOTICE,
    routing_granted_holder_notice, routing_revoked_holder_notice,
    ROUTING_REVOKED_NOTICE, ROUTING_LENT_OUT_NOTE)
from .updates import (
    update_available, release_url, update_wait, UPDATE_NOTHING, UPDATE_STARTING, UPDATES_MUTED_TOAST,
    update_blocked_toast, updates_notify_toast, update_failed, update_applied, update_not_applied, changelog_details, CHANGELOG_URL)
from .client import (
    subscription_block, greeting_guest, held_devices_tail, device_delete_ask, device_card_own, device_card_lent, device_card_held,
    lent_device_deleted_by_holder_notice, lent_device_deleted_by_admin_notice,
    lent_device_reassigned_notice, lent_device_deleted_by_owner_notice,
    friend_device_added, friend_other_donor_refusal, guest_upgraded,
    guest_upgraded_donor_notice, guest_upgraded_admin_tail, GUEST_NO_DEVICES_LEFT,
    block_device_ask, FRIEND_ALREADY_USER, friend_activated,
    friend_activated_host_notice, finish_link, finish_qr, finish_file, CONNECT_METHOD_ASK, FINISH_CLIENT_INVITE, finish_friend_invite,
    friend_invite_message, friend_invite_plain, transfer_ask, client_card,
    subscription_kind_label, pause_credit_line, pause_balance_line,
    pause_rules_details, subscription_text, subscription_status_only, subscription_short, traffic_short, devices_short,
    status_line, rf_status_short, vpn_status_line, greeting_client, devices_header, pick_device_header, limit_exhausted_line,
    device_deleted, device_removed, DELETE_ONLY_DEVICE_WARNING,
    DELETE_DEVICE_CONFIRM, HELP_INTRO, UNMANAGED_DEVICE_EXPLAIN, UNMANAGED_DEVICE_LINE,
    UNMANAGED_DEVICE_DIALOG, limit_changed_notice,
    ACTIVATION_OK, ACTIVATION_OK_HELP, ACTIVATION_INVALID, ACTIVATION_ALREADY,
    COLD_START_GREETING, CODE_NO_ARG, grace_activated_client, GRACE_STALE,
    grace_activated_admin, pause_ask, pause_other_prompt, pause_days_bad,
    pause_emergency_code, pause_entered_summary, pause_unavailable,
    pause_limit_exhausted, pause_resumed_self, PAUSE_WARNING_LINE,
    add_device_prompt, device_created, device_limit_prompt, device_limit_other_prompt,
    device_limit_over, NUMBER_BAD, NAME_EMPTY, limit_note, name_note, device_name_prompt,
    SUB_PAYLOAD, RF_PAYLOAD_CLIENT, DEV_PAYLOAD, device_link)
from .admin import (
    traffic_profiles_text, rf_traffic_line, client_devices_header, RF_PAYLOAD, TRAFFIC_PAYLOAD, UPD_PAYLOAD,
    UNASSIGNED_PAYLOAD, ONLINE_PAYLOAD, EXPIRING_PAYLOAD, month_label,
    online_devices_text, traffic_devices_text, expiring_text, unassigned_text,
    admin_panel, migration_line, my_devices_header, admin_device_card, admin_device_delete_ask,
    device_deleted_note, reassign_ask, reassign_slot_ask, reassigned_note, block_device_ask_admin,
    profiles_header, admin_client_card, client_edit_text, client_name_prompt, client_name_note,
    devs_limit_prompt, devs_limit_note, traffic_limit_prompt, traffic_limit_note,
    OTHER_NUMBER_PROMPT, extend_text, extended_note, period_start_prompt, period_end_prompt,
    PERIOD_BAD, PERIOD_NO_START, period_changed_note, block_client_ask, BLOCK_NOTIFY_ASK,
    blocked_toast, client_delete_ask, client_deleted_note, CLIENT_DELETE_PARTIAL, resumed_note,
    NEW_PROFILE_NAME, new_profile_devs, new_profile_traffic, new_profile_period,
    invite_plain, invite_screen, add_device_prompt_admin,
    device_created_admin, admin_bootstrap_device, reassign_donor_notice,
    reassign_recipient_notice, reassign_recipient_notice_with_slot,
    activated_admin_notice, INVITE_FORWARD_TEMPLATE, LIMIT_REACHED, NUMBER_BAD_LIMIT, limit_reached_line,
    MIGRATION_PAYLOAD, migration_overview_text, migration_client_text,
    TRAFFIC_LIMIT_CLIENT_ASK, TRAFFIC_LIMIT_BAD)
from .settings import (
    SETTINGS_NOTIFY_CLIENTS,
    settings_email_text, EMAIL_ASK_ADDRESS, EMAIL_ASK_IMAP_HOST, EMAIL_ASK_IMAP_PORT,
    EMAIL_ASK_SMTP_HOST, EMAIL_ASK_SMTP_PORT, EMAIL_BAD_ADDRESS, EMAIL_BAD_PORT,
    EMAIL_BAD_HOST, EMAIL_FORGET_CONFIRM, email_forget_confirm, EMAIL_FORGOTTEN, email_test_sent,
    email_ask_address_change, email_ask_resume_address, email_provider_line,
    email_ask_password, email_saved, email_check_failed,
    restore_offer, restore_warning, restore_rejected, RESTORE_STARTED, restore_done,
    EMAIL_NOT_CONFIGURED, BACKUP_NEEDS_ENCRYPTION, backup_needs_encryption, backup_encryption_text,
    BACKUP_ASK_PASSPHRASE, BACKUP_ASK_PASSPHRASE_AGAIN, BACKUP_PASSPHRASE_MISMATCH,
    BACKUP_PASSPHRASE_SET, backup_mailed, SVC_CONFIRM_AWG,
    SVC_CONFIRM_BOT, svc_confirm_awg, svc_confirm_bot, settings_svc_text, settings_upd_text,
    SETTINGS_BOUNDS, SETTINGS_TEXT, UPDATE_SCHEDULE_CYCLE, UPDATE_SCHEDULE_LABELS, PRIVATE_DNS_WHAT, private_dns_offer,
    PRIVATE_DNS_LATER, PRIVATE_DNS_DISMISSED, settings_server_text,
    settings_firewall_text, firewall_confirmed, firewall_rolled_back,
    SSH_PORT_ASK, ssh_port_busy, ssh_port_same, ssh_port_changed, ssh_owner_refusal,
    settings_prompt, settings_changed, settings_ssh_allow_added, settings_bad_value,
    settings_root_text, settings_notify_text, settings_subs_text, settings_mon_text,
    settings_backup_text, EMAIL_ASK_IMAP, EMAIL_ASK_SMTP, EMAIL_CHECK_OK, backup_when_prompt, BACKUP_WHEN_BAD, ssh_port_ask, FIREWALL_ON_ALERT, cycle_toast)
from .broadcast import (
    BROADCAST_EMPTY, BROADCAST_MODE, BROADCAST_TARGETS, BROADCAST_TARGETS_EXTEND, broadcast_targets_text,
    BROADCAST_NO_TARGETS, BROADCAST_ALL_UNLIMITED, BROADCAST_DAYS_BAD,
    subscription_mark, broadcast_days_prompt, extension_header, announcement_text,
    extension_reserve, broadcast_prompt, broadcast_preview, broadcast_preview_photos,
    broadcast_too_many_photos, broadcast_too_long, broadcast_report)
from .gateway import (
    gateway_panel, gateway_health, gateway_transit_text, gateway_transit_ask_domain,
    gateway_transit_result, gateway_transit_removed_toast, gateway_claim_via_channel_text, short_name,
    smb_line, own_lists_short,
    GW_BACKUP_NO_KEY, host_rebooted, GW_CONFIRM_REASSERT, gw_confirm_reassert, SYNC_TAILS,
    GW_BUNDLE_NOT_OURS, GW_FIRST_RUN_FILE,
    GW_BUNDLE_PASSPHRASE_QUESTION, gateway_claim_forward_text, gateway_apply_report,
    gateway_op_result, gateway_config_result, gateway_bundle_received,
    gateway_ssh_text, gateway_ssh_port_changed,
    GW_SSH_ALLOW_ASK, gateway_ssh_allow_added, GW_SSH_ALLOW_ALREADY, GW_SSH_FILTER_ON_ALERT,
    GW_SSH_FILTER_OFF, gateway_ssh_panel_line)
from .common import HB_SERVER_DOWN, HB_SERVER_UP, cancelled, BOT_RESTARTED

__all__ = [
    "_e", "gb", "human_bytes", "used_of_limit", "gb_str", "client_total_line", "device_label",
    "plain_ip", "tg_link", "client_link",
    "owner_name", "holder_name", "owner_link", "holder_link", "_n_devices", "plural_ru", "device_emoji", "device_state", "details", "deep_link", "access_status_line", "profile_link", "admin_device_link", "tree", "sub_line", "rf_value", "updown_brief", "migration_pending_text", "migration_start_confirm",
    "migration_started", "migration_finish_confirm", "migration_cancel_confirm",
    "migration_hello", "migration_ready", "migration_orphans_text",
    "migration_cancelled", "migration_finished", "migration_prepare_intro",
    "MIGRATION_ASK_PORT", "migration_prepared", "migration_prepare_failed",
    "migration_generation_pending", "migration_needed", "migration_promoted",
    "gateway_device_card", "gateway_claim_marked",
    "gateway_claim_already", "ROUTING_NAME", "SETTINGS_ROUTING_ABSENT",
    "drift_lines", "GW_CARD_PAYLOAD", "agent_bot_line", "gateway_bundle_caption", "gateway_plain_bundle_caption", "gateway_installed_text", "gateway_bundle_applied_text",
    "GATEWAY_CHOOSE_INTRO", "GATEWAY_PICK_INTRO",
    "GATEWAY_PICK_EMPTY", "gateway_new_ask", "gateway_mark_ask", "gateway_remove_ask", "gateway_removed", "ROUTING_PROVISION_INTRO",
    "routing_provisioned", "routing_provision_failed", "gateway_ask_token",
    "ping_fmt", "ping_line", "ext_ip_line", "vps_hostname", "gateway_role_line", "slot_name",
    "slot_short", "slot_status",
    "gateway_card_text", "gateways_text", "slot_line", "channel_lines", "gateway_edit_text", "routing_params_text", "GATEWAYS_OFF", "GATEWAYS_ABOUT", "GATEWAYS_AUTO_OFF", "ROUTER_TABS",
    "GATEWAY_STANDBY_CHOOSE_INTRO", "gateway_replace_intro", "gateway_switch_ask",
    "gateway_home_text", "gateway_home_report", "gateway_label_text",
    "GW_INSTALL_URL", "gateway_install_instructions", "gateway_lan_ask", "already_state", "GW_TOKEN_NOT_FORGOTTEN", "gateway_peer_ask", "peer_nets_line", "GATEWAY_LAN_NO_SUBNET", "gateway_router_text", "ROUTER_IP_PLACEHOLDER", "SETTINGS_ROUTING_SUBOFF",
    "routing_users_text", "routing_status_line", "routing_admin_status_line", "ROUTING_ABOUT",
    "ROUTING_ABOUT_OFF", "ROUTING_ADD_PROMPT", "ROUTING_APPLY_HINT",
    "ROUTING_ADDED_HINT", "routing_domain_removed", "routing_panel_text",
    "routing_add_report", "routing_sites_text", "routing_clear_ask", "ROUTING_CLEAR_CONFIRM", "ROUTING_UNAVAILABLE", "ROUTING_NOT_ALLOWED_ADMIN",
    "routing_gateway_warning", "ROUTING_DISABLE_CONFIRM", "ROUTING_GRANTED_NOTICE",
    "routing_granted_holder_notice", "routing_revoked_holder_notice",
    "ROUTING_REVOKED_NOTICE", "ROUTING_LENT_OUT_NOTE", "update_available", "update_wait", "UPDATE_NOTHING", "UPDATE_STARTING", "UPDATES_MUTED_TOAST",
    "update_blocked_toast", "updates_notify_toast", "update_failed", "update_applied", "changelog_details", "CHANGELOG_URL",
    "update_not_applied", "subscription_block", "greeting_guest", "held_devices_tail", "device_delete_ask", "device_card_own", "device_card_lent", "device_card_held", "lent_device_deleted_by_holder_notice", "lent_device_deleted_by_admin_notice", "lent_device_reassigned_notice", "lent_device_deleted_by_owner_notice", "friend_device_added", "friend_other_donor_refusal", "guest_upgraded", "guest_upgraded_donor_notice", "guest_upgraded_admin_tail", "GUEST_NO_DEVICES_LEFT", "block_device_ask", "FRIEND_ALREADY_USER", "friend_activated", "friend_activated_host_notice", "finish_link", "finish_qr", "finish_file", "CONNECT_METHOD_ASK", "FINISH_CLIENT_INVITE", "finish_friend_invite", "friend_invite_message", "friend_invite_plain", "transfer_ask", "client_card", "subscription_kind_label", "pause_credit_line", "pause_balance_line", "pause_rules_details", "subscription_text", "subscription_status_only", "subscription_short", "traffic_short", "devices_short", "status_line", "rf_status_short", "vpn_status_line", "greeting_client", "devices_header", "pick_device_header", "limit_exhausted_line", "device_deleted", "device_removed", "DELETE_ONLY_DEVICE_WARNING", "DELETE_DEVICE_CONFIRM", "HELP_INTRO", "UNMANAGED_DEVICE_EXPLAIN", "UNMANAGED_DEVICE_LINE", "UNMANAGED_DEVICE_DIALOG", "limit_changed_notice", "ACTIVATION_OK", "ACTIVATION_OK_HELP", "ACTIVATION_INVALID", "ACTIVATION_ALREADY", "COLD_START_GREETING", "CODE_NO_ARG", "grace_activated_client", "GRACE_STALE", "grace_activated_admin", "pause_ask", "pause_other_prompt", "pause_days_bad", "pause_emergency_code", "pause_entered_summary", "pause_unavailable", "pause_limit_exhausted", "pause_resumed_self", "PAUSE_WARNING_LINE", "add_device_prompt", "device_created", "device_limit_prompt", "device_limit_other_prompt", "device_limit_over", "NUMBER_BAD", "NAME_EMPTY", "limit_note", "name_note", "device_name_prompt", "SUB_PAYLOAD", "RF_PAYLOAD_CLIENT", "DEV_PAYLOAD", "device_link",
    "traffic_profiles_text", "rf_traffic_line", "client_devices_header", "RF_PAYLOAD", "TRAFFIC_PAYLOAD", "UPD_PAYLOAD", "UNASSIGNED_PAYLOAD", "ONLINE_PAYLOAD", "EXPIRING_PAYLOAD", "month_label", "online_devices_text", "traffic_devices_text", "expiring_text", "unassigned_text", "admin_panel", "migration_line", "my_devices_header", "admin_device_card", "admin_device_delete_ask", "device_deleted_note", "reassign_ask", "reassign_slot_ask", "reassigned_note", "block_device_ask_admin", "profiles_header", "admin_client_card", "client_edit_text", "client_name_prompt", "client_name_note", "devs_limit_prompt", "devs_limit_note", "traffic_limit_prompt", "traffic_limit_note", "OTHER_NUMBER_PROMPT", "extend_text", "extended_note", "period_start_prompt", "period_end_prompt", "PERIOD_BAD", "PERIOD_NO_START", "period_changed_note", "block_client_ask", "BLOCK_NOTIFY_ASK", "blocked_toast", "client_delete_ask", "client_deleted_note", "CLIENT_DELETE_PARTIAL", "resumed_note", "NEW_PROFILE_NAME", "new_profile_devs", "new_profile_traffic", "new_profile_period", "invite_plain", "invite_screen", "add_device_prompt_admin", "device_created_admin", "admin_bootstrap_device", "reassign_donor_notice", "reassign_recipient_notice", "reassign_recipient_notice_with_slot", "activated_admin_notice", "INVITE_FORWARD_TEMPLATE", "LIMIT_REACHED", "NUMBER_BAD_LIMIT", "limit_reached_line", "MIGRATION_PAYLOAD", "migration_overview_text", "migration_client_text", "release_url", "TRAFFIC_LIMIT_CLIENT_ASK", "TRAFFIC_LIMIT_BAD",
    "SETTINGS_NOTIFY_CLIENTS",
    "settings_email_text", "EMAIL_ASK_ADDRESS", "EMAIL_ASK_IMAP_HOST",
    "EMAIL_ASK_IMAP_PORT", "EMAIL_ASK_SMTP_HOST", "EMAIL_ASK_SMTP_PORT",
    "EMAIL_BAD_ADDRESS", "EMAIL_BAD_PORT", "EMAIL_BAD_HOST", "EMAIL_FORGET_CONFIRM", "email_forget_confirm",
    "EMAIL_FORGOTTEN", "email_test_sent", "email_ask_address_change",
    "email_ask_resume_address", "email_provider_line", "email_ask_password",
    "email_saved", "email_check_failed",
    "restore_offer", "restore_warning", "restore_rejected", "RESTORE_STARTED", "restore_done",
    "EMAIL_NOT_CONFIGURED", "BACKUP_NEEDS_ENCRYPTION", "backup_needs_encryption", "backup_encryption_text",
    "BACKUP_ASK_PASSPHRASE", "BACKUP_ASK_PASSPHRASE_AGAIN",
    "BACKUP_PASSPHRASE_MISMATCH", "BACKUP_PASSPHRASE_SET", "backup_mailed",
    "SVC_CONFIRM_AWG", "SVC_CONFIRM_BOT", "svc_confirm_awg", "svc_confirm_bot", "settings_svc_text",
    "settings_upd_text", "SETTINGS_BOUNDS", "SETTINGS_TEXT", "UPDATE_SCHEDULE_CYCLE", "UPDATE_SCHEDULE_LABELS",
    "PRIVATE_DNS_WHAT", "private_dns_offer", "PRIVATE_DNS_LATER",
    "PRIVATE_DNS_DISMISSED", "settings_server_text", "settings_firewall_text",
    "firewall_confirmed", "firewall_rolled_back", "settings_prompt",
    "SSH_PORT_ASK", "ssh_port_busy", "ssh_port_same", "ssh_port_changed", "ssh_owner_refusal",
    "settings_changed", "settings_ssh_allow_added", "settings_bad_value",
    "settings_root_text", "settings_notify_text", "settings_subs_text", "settings_mon_text", "settings_backup_text", "EMAIL_ASK_IMAP", "EMAIL_ASK_SMTP", "EMAIL_CHECK_OK", "backup_when_prompt", "BACKUP_WHEN_BAD", "ssh_port_ask", "FIREWALL_ON_ALERT", "cycle_toast",
    "BROADCAST_EMPTY", "BROADCAST_MODE", "broadcast_targets_text", "BROADCAST_TARGETS",
    "BROADCAST_TARGETS_EXTEND", "BROADCAST_NO_TARGETS", "BROADCAST_ALL_UNLIMITED",
    "BROADCAST_DAYS_BAD", "subscription_mark", "broadcast_days_prompt",
    "extension_header", "announcement_text", "extension_reserve", "broadcast_prompt",
    "broadcast_preview", "broadcast_preview_photos", "broadcast_too_many_photos",
    "broadcast_too_long", "broadcast_report", "gateway_panel", "gateway_health",
    "gateway_transit_text", "gateway_transit_ask_domain", "gateway_transit_result", "gateway_transit_removed_toast", "smb_line", "own_lists_short", "GW_SSH_FILTER_ON_ALERT",
    "gateway_claim_via_channel_text",
    "short_name", "GW_BACKUP_NO_KEY",
    "host_rebooted", "GW_CONFIRM_REASSERT", "gw_confirm_reassert", "SYNC_TAILS",
    "GW_BUNDLE_NOT_OURS", "GW_FIRST_RUN_FILE",
    "GW_BUNDLE_PASSPHRASE_QUESTION", "gateway_claim_forward_text",
    "gateway_ssh_text", "gateway_ssh_port_changed",
    "GW_SSH_ALLOW_ASK", "gateway_ssh_allow_added", "GW_SSH_ALLOW_ALREADY",
    "GW_SSH_FILTER_OFF",
    "gateway_ssh_panel_line",
    "gateway_apply_report", "gateway_op_result", "gateway_config_result",
    "gateway_bundle_received", "HB_SERVER_DOWN", "HB_SERVER_UP", "cancelled",
    "BOT_RESTARTED",
]
