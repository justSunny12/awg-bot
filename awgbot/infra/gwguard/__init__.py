"""
gwguard/ — сторона ШЛЮЗА (части: common — константы и вызов nft; unit — юнит обвязки и
локальное состояние; nft — таблица и наборы; wg — аплинк и политика; uplinktxn —
конфиг аплинка режимами --uplink/--uplink-commit/--uplink-revert и замок
обвязки на всю транзакцию; lan — локальная сеть и сервисы): чтение таблицы `inet awg_gw_guard`, которую ставит
routing-gw-setup.sh (юнит awg-link-gw.service), и локальные добавки к ней.

Генератор таблицы — ОДИН, скрипт: он же реассертит её при загрузке и по
`systemctl restart awg-link-gw.service`. Агент таблицу не пишет — только
читает (проверки, панель) и просит перевыставить; единственное исключение —
наборы с динамикой (`ssh_allow4`, `server4`: резолв имён DynDNS), которые
агент наполняет сам через `set_sync`, как основной бот держит admin4.

Юнит обвязки агент правит только по данным канала линка
(`unit_set_env`/`unit_restore`): настройки (агенту 3.2.x — четыре ключа
`gwlink.SETTINGS_KEYS`, с документом конфигурации — `gwcfg.ENV_KEYS`) с
перезапуском юнита и ключи признания аплинка (`GATEWAY_PUBKEY`,
`GATEWAY_PREV_PUBKEY`) — под замком обвязки `apply_lock`; при отказе
возвращает прежний текст. Сверх этого — строка `AWG_GW_UNIT_RUN=1` (по ней
прогон юнита после замка сверяет свои строки с диском): юниту прежнего
образца агент дописывает её сам, не дожидаясь полного прогона скрипта. Фиды, привезённые каналом, лежат в /var/lib/awg-gw/feed и
применяются тем же скриптом списков с AWG_LAN_FROM.

Локальное состояние файервола шлюза — /etc/awg-gw/firewall.env, его читают
скрипт и юнит (EnvironmentFile): ADMIN_IPS_EXTRA (доверенные из туннеля сверх
бандла, `awg-bot firewall allow`), SSH_PORT (факт: порт, который слушает
sshd — пишет агент), SSH_FILTER, SSH_ALLOW, SSH_ALLOW_RESOLVED (SSH снаружи,
раздел «Доступ по SSH» агента и `awg-bot ssh …`).
"""
from __future__ import annotations

import subprocess   # noqa: F401 — тесты подменяют subprocess.run через пакет
from pathlib import Path   # noqa: F401 — тесты подменяют Path через пакет

from awgbot.core import config   # noqa: F401

from .common import TABLE_FAMILY, TABLE_NAME, TABLE, FW_ENV, CHAINS, GwGuardError, _nft, ENV_KEYS, _ENV_HEAD, _ENV_LINE_RE, _HOST_RE, _ADDR_CHARS_RE, TIMEOUT_MARK, STATUS_FILE, UPLINK_TABLE, TG_MARK, HOME_TABLE_NAME, DNSMASQ_D, LAN_STATUS_FILE, LAN_LISTS_SCRIPT, LAN_DOMAIN_SCRIPT, OWN_LIST_FILES, OWN_LISTS_NEW, OWN_FILL_NEW, OWN_FILL_TIMEOUT, LAN_SCRIPT_TIMEOUT, LAN_FEED_DIR, LAN_SERVICES_SCRIPT, PEER_SERVICES_CONF_NAME, PEER_SERVICES_CONF, PEER_SERVICES_NEW, _OS_ERRORS, os_error_text, SETUP_SCRIPT, LINK_APPLY_TIMEOUT, GW_ETC
from .nft import table_info, iptables_forward_policy, set_sync, collapse, overlaps, home_table_info, forward_accepts
from .unit import read_env, _check_env_value, write_env, read_extra, write_extra, plumbing_installed, lan_nets, _norm_elem, server_host, _unit_cache, unit_text, unit_admin_ips, unit_state, unit_path, unit_set_env, _write_private, _daemon_reload, unit_restore, is_timeout, reassert, unit_env, lan_mode
from .wg import _awg, _endpoint_host, uplink_interface, uplink_pubkey, script_status, _ip_json, default_route, default_route_dev, default_route_via, setup_script_has, _fwmark_of, uplink_policy, uplink_policy_ensure, client_subnet, handshake_age, handshake_state, link_apply, link_applied_how, _setup_env
from .uplinktxn import APPLY_LOCK_PATH, apply_lock, uplink_apply, uplink_finish, uplink_applied_how, uplink_conf_path
from .lan import lists_status, lan_own_lists, dnsmasq_active, upstream_stats, iface_for_subnet, run_lan_lists, avahi_browse_available, avahi_active, avahi_browse, run_lan_services, dns_local, lan_domain_has_sync, run_lan_domain

__all__ = ["_setup_env", "uplink_apply", "uplink_conf_path", "APPLY_LOCK_PATH", "apply_lock", "uplink_finish", "uplink_applied_how", "handshake_state", "SETUP_SCRIPT", "LINK_APPLY_TIMEOUT", "GW_ETC", "handshake_age", "link_apply", "link_applied_how", "TABLE_FAMILY", "TABLE_NAME", "TABLE", "FW_ENV", "CHAINS", "GwGuardError", "_nft", "ENV_KEYS", "_ENV_HEAD", "_ENV_LINE_RE", "_HOST_RE", "_ADDR_CHARS_RE", "TIMEOUT_MARK", "STATUS_FILE", "UPLINK_TABLE", "TG_MARK", "HOME_TABLE_NAME", "DNSMASQ_D", "LAN_STATUS_FILE", "LAN_LISTS_SCRIPT", "LAN_DOMAIN_SCRIPT", "OWN_LIST_FILES", "OWN_LISTS_NEW", "OWN_FILL_NEW", "OWN_FILL_TIMEOUT", "LAN_SCRIPT_TIMEOUT", "LAN_FEED_DIR", "LAN_SERVICES_SCRIPT", "PEER_SERVICES_CONF_NAME", "PEER_SERVICES_CONF", "PEER_SERVICES_NEW", "_OS_ERRORS", "os_error_text", "table_info", "iptables_forward_policy", "set_sync", "collapse", "overlaps", "home_table_info", "forward_accepts", "read_env", "_check_env_value", "write_env", "read_extra", "write_extra", "plumbing_installed", "lan_nets", "_norm_elem", "server_host", "_unit_cache", "unit_text", "unit_admin_ips", "unit_state", "unit_path", "unit_set_env", "_write_private", "_daemon_reload", "unit_restore", "is_timeout", "reassert", "unit_env", "lan_mode", "_awg", "_endpoint_host", "uplink_interface", "uplink_pubkey", "script_status", "_ip_json", "default_route", "default_route_dev", "default_route_via", "setup_script_has", "_fwmark_of", "uplink_policy", "uplink_policy_ensure", "client_subnet", "lists_status", "lan_own_lists", "dnsmasq_active", "upstream_stats", "iface_for_subnet", "run_lan_lists", "avahi_browse_available", "avahi_active", "avahi_browse", "run_lan_services", "dns_local", "lan_domain_has_sync", "run_lan_domain", "Path", "subprocess", "config"]
