"""gateway/ — тексты роли gateway (части: common — мелкие помощники панели и списков;
panel — панель агента и монитор здоровья; transit — VPN-транзит, свои списки, SMB;
bundle — файл конфигурации, пометка, итоги применения; access — раздел «🛡 SSH-доступ»;
ask — вопросы о почте и фразе с сервера AWG, их итоги, строка «⏳ Ждут решения»)."""
from __future__ import annotations

from .common import _gw_link_short, _uniq, _gw_health_summary
from .panel import _channel_view, channel_panel_line, _gw_role, gateway_panel, gateway_health
from .transit import _packets, _lists_counts, _lists_updated_short, smb_line, lan_about, gateway_transit_text, gateway_transit_ask_domain, own_lists_short, own_lists_state_line, SYNC_TAILS, gateway_transit_removed_toast, short_name, gateway_transit_result
from .bundle import GW_BACKUP_NO_KEY, host_rebooted, gw_confirm_reassert, GW_FIRST_RUN_FILE, gw_bundle_not_ours, GW_BUNDLE_PASSPHRASE_QUESTION, gateway_claim_via_channel_text, _claim_head, gateway_claim_forward_text, gateway_apply_report, gateway_config_result, gateway_op_result, gateway_bundle_received
from .access import _owner_name, gateway_ssh_text, gateway_ssh_port_changed, GW_SSH_ALLOW_ASK, gateway_ssh_allow_added, GW_SSH_ALLOW_ALREADY, GW_SSH_FILTER_ON_ALERT, GW_SSH_FILTER_OFF, gateway_ssh_panel_line
from .ask import QUESTION_HUMAN, QUESTION_STALE, LINK_KEYS_ACCEPTED, UPLINK_APPLYING, UPLINK_BUSY, QUESTION_ANSWERED, GW_TOKEN_SWITCHED, cfg_question_text, cfg_answer_text, cfg_pending_line

__all__ = ["QUESTION_HUMAN", "QUESTION_STALE", "LINK_KEYS_ACCEPTED", "UPLINK_APPLYING", "UPLINK_BUSY", "QUESTION_ANSWERED", "GW_TOKEN_SWITCHED", "cfg_question_text", "cfg_answer_text", "cfg_pending_line", "_gw_link_short", "_uniq", "_gw_health_summary", "_channel_view", "channel_panel_line", "_gw_role", "gateway_panel", "gateway_health", "_packets", "_lists_counts", "_lists_updated_short", "smb_line", "lan_about", "gateway_transit_text", "gateway_transit_ask_domain", "own_lists_short", "own_lists_state_line", "SYNC_TAILS", "gateway_transit_removed_toast", "short_name", "gateway_transit_result", "GW_BACKUP_NO_KEY", "host_rebooted", "gw_confirm_reassert", "GW_FIRST_RUN_FILE", "gw_bundle_not_ours", "GW_BUNDLE_PASSPHRASE_QUESTION", "gateway_claim_via_channel_text", "_claim_head", "gateway_claim_forward_text", "gateway_apply_report", "gateway_config_result", "gateway_op_result", "gateway_bundle_received", "_owner_name", "gateway_ssh_text", "gateway_ssh_port_changed", "GW_SSH_ALLOW_ASK", "gateway_ssh_allow_added", "GW_SSH_ALLOW_ALREADY", "GW_SSH_FILTER_ON_ALERT", "GW_SSH_FILTER_OFF", "gateway_ssh_panel_line"]
