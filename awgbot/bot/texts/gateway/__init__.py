"""gateway/ — тексты роли gateway (части: common — мелкие помощники панели и списков;
panel — панель агента и монитор здоровья; transit — VPN-транзит, свои списки, SMB;
bundle — файлы в чате, восстановление, перезагрузка; access — раздел «🛡 SSH-доступ»;
ask — вопросы о почте и фразе с сервера AWG, их итоги, строка «⏳ Ждут решения»)."""
from __future__ import annotations

from .common import _gw_link_short, _uniq, _gw_health_summary, mail_why, MAIL_FIX_TAIL
from .panel import _channel_view, channel_panel_line, gateway_wait_line, _gw_role, gateway_panel, gateway_health, refreshed_line, REFRESH_PAYLOAD
from .transit import _packets, _lists_counts, _lists_updated_short, smb_line, lan_about, gateway_transit_text, gateway_transit_ask_domain, own_lists_short, own_lists_state_line, SYNC_TAILS, gateway_transit_removed_toast, short_name, gateway_transit_result
from .bundle import GW_BACKUP_NO_KEY, host_rebooted, gw_confirm_reassert, REASSERT_FAIL_HINT, GW_REISSUE_FILE_REFUSED, gateway_op_result
from .access import _owner_name, gateway_ssh_text, gateway_ssh_port_changed, GW_SSH_ALLOW_ASK, gateway_ssh_allow_added, GW_SSH_ALLOW_ALREADY, GW_SSH_FILTER_ON_ALERT, GW_SSH_FILTER_ON_EMPTY_ALERT, GW_SSH_FILTER_OFF, gateway_ssh_removed, gateway_ssh_panel_line
from .ask import QUESTION_HUMAN, QUESTION_STALE, LINK_KEYS_ACCEPTED, UPLINK_APPLYING, UPLINK_BUSY, QUESTION_ANSWERED, QUESTIONS_NONE, KEEP_LABEL, ACCEPT_LABEL, GW_TOKEN_SWITCHED, cfg_question_text, cfg_answer_text, cfg_pending_line

__all__ = ["mail_why", "MAIL_FIX_TAIL", "refreshed_line", "REFRESH_PAYLOAD", "REASSERT_FAIL_HINT", "QUESTIONS_NONE", "KEEP_LABEL", "ACCEPT_LABEL", "QUESTION_HUMAN", "QUESTION_STALE", "LINK_KEYS_ACCEPTED", "UPLINK_APPLYING", "UPLINK_BUSY", "QUESTION_ANSWERED", "GW_TOKEN_SWITCHED", "cfg_question_text", "cfg_answer_text", "cfg_pending_line", "_gw_link_short", "_uniq", "_gw_health_summary", "_channel_view", "channel_panel_line", "gateway_wait_line", "_gw_role", "gateway_panel", "gateway_health", "_packets", "_lists_counts", "_lists_updated_short", "smb_line", "lan_about", "gateway_transit_text", "gateway_transit_ask_domain", "own_lists_short", "own_lists_state_line", "SYNC_TAILS", "gateway_transit_removed_toast", "short_name", "gateway_transit_result", "GW_BACKUP_NO_KEY", "host_rebooted", "gw_confirm_reassert", "GW_REISSUE_FILE_REFUSED", "gateway_op_result", "_owner_name", "gateway_ssh_text", "gateway_ssh_port_changed", "GW_SSH_ALLOW_ASK", "gateway_ssh_allow_added", "GW_SSH_ALLOW_ALREADY", "GW_SSH_FILTER_ON_ALERT", "GW_SSH_FILTER_ON_EMPTY_ALERT", "GW_SSH_FILTER_OFF", "gateway_ssh_removed", "gateway_ssh_panel_line"]
