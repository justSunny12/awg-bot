"""Экраны администратора: панель, списки, трафик списком, карточки профиля и устройства, создание и продление профилей, привязка устройств.

Части: common — payload'ы экранов, месяц, итог трафика, нарезка списков;
panel — главная (шапка, переезд); lists — списки онлайн, истекающих, без
профиля и трафик списком; devices — мои устройства, карточка устройства,
перенос и блокировка; profiles — карточка профиля, правка, продление, лимиты,
блокировка, удаление; create — новый профиль, приглашение, устройство
профилю; notices — уведомления."""

from __future__ import annotations

# имя хоста кэшируется на процесс; тесты подменяют его через пакет
_HOSTNAME: str | None = None

from .common import RF_PAYLOAD, TRAFFIC_PAYLOAD, UPD_PAYLOAD, UNASSIGNED_PAYLOAD, ONLINE_PAYLOAD, EXPIRING_PAYLOAD, month_label, _total, _LIST_SEP, _LIST_CAP, _more, LIMIT_REACHED
from .panel import _hostname, rf_traffic_line, admin_panel, MIGRATION_PAYLOAD, migration_line, _mig_dot, migration_overview_text, migration_client_text
from .lists import online_devices_text, expiring_text, unassigned_text, _OUTSIDE, _rf_sub, traffic_profiles_text, traffic_devices_text
from .devices import my_devices_header, _admin_usage, client_devices_header, admin_device_card, admin_device_delete_ask, device_deleted_note, _owner_tail, reassign_ask, reassign_slot_ask, limit_reached_line, reassigned_note, block_device_ask_admin
from .profiles import profiles_header, _pause_of, _KIND_SHORT, _sub_line, _rf_short, admin_client_card, client_edit_text, client_name_prompt, client_name_note, devs_limit_prompt, devs_limit_note, traffic_limit_prompt, traffic_limit_note, OTHER_NUMBER_PROMPT, NUMBER_BAD_LIMIT, NAME_EMPTY, extend_text, _PERIOD_ACC, extended_note, period_start_prompt, period_end_prompt, PERIOD_BAD, PERIOD_NO_START, period_changed_note, block_client_ask, BLOCK_NOTIFY_ASK, blocked_toast, client_delete_ask, client_deleted_note, CLIENT_DELETE_PARTIAL, resumed_note
from .create import NEW_PROFILE_NAME, new_profile_devs, new_profile_traffic, new_profile_period, INVITE_FORWARD_TEMPLATE, invite_plain, invite_screen, add_device_prompt_admin, device_created_admin, admin_bootstrap_device
from .notices import _slots_phrase, reassign_donor_notice, reassign_recipient_notice, reassign_recipient_notice_with_slot, activated_admin_notice

__all__ = ["RF_PAYLOAD", "TRAFFIC_PAYLOAD", "UPD_PAYLOAD", "UNASSIGNED_PAYLOAD", "ONLINE_PAYLOAD", "EXPIRING_PAYLOAD", "month_label", "_total", "_LIST_SEP", "_LIST_CAP", "_more", "LIMIT_REACHED", "_hostname", "rf_traffic_line", "admin_panel", "MIGRATION_PAYLOAD", "migration_line", "_mig_dot", "migration_overview_text", "migration_client_text", "online_devices_text", "expiring_text", "unassigned_text", "_OUTSIDE", "_rf_sub", "traffic_profiles_text", "traffic_devices_text", "my_devices_header", "_admin_usage", "client_devices_header", "admin_device_card", "admin_device_delete_ask", "device_deleted_note", "_owner_tail", "reassign_ask", "reassign_slot_ask", "limit_reached_line", "reassigned_note", "block_device_ask_admin", "profiles_header", "_pause_of", "_KIND_SHORT", "_sub_line", "_rf_short", "admin_client_card", "client_edit_text", "client_name_prompt", "client_name_note", "devs_limit_prompt", "devs_limit_note", "traffic_limit_prompt", "traffic_limit_note", "OTHER_NUMBER_PROMPT", "NUMBER_BAD_LIMIT", "NAME_EMPTY", "extend_text", "_PERIOD_ACC", "extended_note", "period_start_prompt", "period_end_prompt", "PERIOD_BAD", "PERIOD_NO_START", "period_changed_note", "block_client_ask", "BLOCK_NOTIFY_ASK", "blocked_toast", "client_delete_ask", "client_deleted_note", "CLIENT_DELETE_PARTIAL", "resumed_note", "NEW_PROFILE_NAME", "new_profile_devs", "new_profile_traffic", "new_profile_period", "INVITE_FORWARD_TEMPLATE", "invite_plain", "invite_screen", "add_device_prompt_admin", "device_created_admin", "admin_bootstrap_device", "_slots_phrase", "reassign_donor_notice", "reassign_recipient_notice", "reassign_recipient_notice_with_slot", "activated_admin_notice"]
