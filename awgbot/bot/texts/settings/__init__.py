"""Экран настроек: разделы, почта, бэкапы, сервер, SSH-доступ, границы значений, приглашения и итоги ввода.

Части: root — корень, уведомления, подписки, мониторинг; email — раздел
«✉️ E-mail»; backup — бэкапы и восстановление; service — «🔧 Сервис» и
«⬆️ Обновления»; server — раздел сервера и свой DNS-резолвер; firewall —
«🛡 SSH-доступ» и порт SSH; inputs — границы и подписи ключей, приглашения к
вводу и итоги, всплывашки циклов."""

from __future__ import annotations

from .root import settings_root_text, SETTINGS_NOTIFY_CLIENTS, settings_notify_text, settings_subs_text, settings_mon_text
from .email import settings_email_text, EMAIL_ASK_ADDRESS, EMAIL_ASK_IMAP, EMAIL_ASK_SMTP, EMAIL_ASK_IMAP_PORT, EMAIL_ASK_SMTP_PORT, EMAIL_BAD_ADDRESS, EMAIL_BAD_PORT, EMAIL_BAD_HOST, email_forget_confirm, EMAIL_FORGOTTEN, email_test_sent, EMAIL_CHECK_OK, email_ask_address_change, email_ask_resume_address, EMAIL_RESUME_ALREADY_SELF, email_provider_line, email_ask_password, email_saved, email_check_failed
from .backup import settings_backup_text, restore_offer_plan, restore_rejected, restore_failed, RESTORE_ALREADY, RESTORE_OTHER_FILE, RESTORE_STARTED, restore_done_report, email_not_configured, backup_needs_encryption, backup_encryption_text, BACKUP_ASK_PASSPHRASE, BACKUP_ASK_PASSPHRASE_AGAIN, BACKUP_PASSPHRASE_MISMATCH, BACKUP_PASSPHRASE_SET, backup_when_prompt, backup_when_changed, BACKUP_WHEN_BAD, backup_mailed, backup_not_sent
from .service import svc_confirm_awg, svc_confirm_bot, settings_svc_text, settings_upd_text, UPDATE_SCHEDULE_CYCLE, UPDATE_SCHEDULE_LABELS
from .inputs import SETTINGS_BOUNDS, _bounds, SETTINGS_TEXT, _ADDRESS_KEYS, _setting_value, settings_prompt, bonus_prompt, BONUS_BAD, bonus_changed, unit_suffix, settings_changed, settings_ssh_allow_added, settings_bad_value, toggle_toast, cycle_toast
from .server import _looks_like_domain, _private_dns_note, PRIVATE_DNS_WHAT, PRIVATE_DNS_BLOCKED_LINE, private_dns_offer, PRIVATE_DNS_LATER, PRIVATE_DNS_DISMISSED, private_dns_blocked_toast, settings_server_text
from .firewall import address_list_line, warnings_block, settings_firewall_text, ssh_owner_refusal, ssh_port_ask, ssh_port_busy, ssh_port_same, ssh_port_changed, firewall_confirmed, firewall_rolled_back, FIREWALL_ON_ALERT

__all__ = [ "settings_root_text", "SETTINGS_NOTIFY_CLIENTS", "settings_notify_text", "settings_subs_text",
    "settings_mon_text", "settings_email_text", "EMAIL_ASK_ADDRESS", "EMAIL_ASK_IMAP", "EMAIL_ASK_SMTP",
    "EMAIL_ASK_IMAP_PORT", "EMAIL_ASK_SMTP_PORT", "EMAIL_BAD_ADDRESS", "EMAIL_BAD_PORT", "EMAIL_BAD_HOST",
    "email_forget_confirm", "EMAIL_FORGOTTEN", "email_test_sent", "EMAIL_CHECK_OK", "email_ask_address_change",
    "email_ask_resume_address", "EMAIL_RESUME_ALREADY_SELF", "email_provider_line", "email_ask_password", "email_saved", "email_check_failed",
    "settings_backup_text", "restore_offer_plan", "restore_rejected", "restore_failed", "RESTORE_ALREADY", "RESTORE_OTHER_FILE",
    "RESTORE_STARTED", "restore_done_report", "email_not_configured", "backup_needs_encryption", "backup_encryption_text",
    "BACKUP_ASK_PASSPHRASE", "BACKUP_ASK_PASSPHRASE_AGAIN", "BACKUP_PASSPHRASE_MISMATCH", "BACKUP_PASSPHRASE_SET",
    "backup_when_prompt", "backup_when_changed", "BACKUP_WHEN_BAD", "backup_mailed", "backup_not_sent", "svc_confirm_awg", "svc_confirm_bot",
    "settings_svc_text", "settings_upd_text", "UPDATE_SCHEDULE_CYCLE", "UPDATE_SCHEDULE_LABELS", "SETTINGS_BOUNDS",
    "_bounds", "SETTINGS_TEXT", "_ADDRESS_KEYS", "_setting_value", "settings_prompt", "bonus_prompt", "BONUS_BAD",
    "bonus_changed", "unit_suffix", "settings_changed", "settings_ssh_allow_added", "settings_bad_value",
    "cycle_toast", "_looks_like_domain", "_private_dns_note", "PRIVATE_DNS_WHAT", "private_dns_offer",
    "PRIVATE_DNS_LATER", "PRIVATE_DNS_DISMISSED", "settings_server_text", "address_list_line", "warnings_block",
    "settings_firewall_text", "ssh_owner_refusal", "ssh_port_ask", "ssh_port_busy", "ssh_port_same",
    "ssh_port_changed", "firewall_confirmed", "firewall_rolled_back", "FIREWALL_ON_ALERT",
    "toggle_toast", "PRIVATE_DNS_BLOCKED_LINE", "private_dns_blocked_toast"]
