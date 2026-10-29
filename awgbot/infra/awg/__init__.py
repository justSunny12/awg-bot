"""
awg/ — единственный слой взаимодействия с сервером AmneziaWG.

Части: common — подавление самозаписи, замок правки конфига, AwgError,
валидация; seam — запуск команд (`_run`, `_exec*`), чтение и запись файлов;
parsers — чистые парсеры conf и dump; params — живые параметры сервера,
топология, ключи; peers — применение конфига, добавление и удаление пира;
blocks — блокировки iptables и снятие прежних ворот SSH; server — статистика,
статус сервиса, перезапуск.

Все команды awg и правки его файлов идут через одну функцию `_exec` —
единственный вход, а не разбросаны по вызовам.

Модуль делится на:
  • чистые парсеры (parse_*) — без сервера, тестируются против реальных выводов;
  • функции, дёргающие сервер (read_*, add_peer, block_ip, show_dump, ...).

Крипто-материал (обфускация, серверный pubkey, psk, ListenPort) читается ЖИВЫМ
из файлов интерфейса — не хардкодится, чтобы переустановка сервера не ломала
конфиги молча.
"""

from __future__ import annotations

import subprocess   # noqa: F401 — тесты подменяют subprocess.run через пакет

from awgbot.core import config   # noqa: F401

from .common import log, _writing, mutation_lock, writing, is_writing, last_self_write, AwgError, _RE_PUBKEY, _RE_IP, _validate_key, _validate_ip
from .seam import _run, _exec, _exec_i, _exec_sh, read_file, write_file
from .parsers import _OBFUSCATION_KEYS, _extract_param, parse_interface_params, parse_occupied_ips, parse_dump, _split_conf, _build_conf, _peer_block
from .params import _SEP, _params_lock, _params_cache, _params_cached_at, PARAMS_TTL_SECONDS, iface_of, conf_path, invalidate_server_params, read_server_params, read_occupied_ips, detect_topology, gen_keypair, pubkey_of
from .peers import apply_config, _backup_conf, _restore_conf, add_peer, remove_peer
from .addrs import ensure_second_address, drop_second_address, with_second, without
from .blocks import block_ip, unblock_ip, blocked_ips, is_blocked, _LEGACY_SSH_CHAIN, gated_ifaces, remove_legacy_ssh_gate
from .server import show_dump, awg_responding, watch_root, _boot_time_iso, service_started_at, restart_server

__all__ = ["log", "_writing", "mutation_lock", "writing", "is_writing", "last_self_write", "AwgError", "_RE_PUBKEY", "_RE_IP", "_validate_key", "_validate_ip", "_run", "_exec", "_exec_i", "_exec_sh", "read_file", "write_file", "_OBFUSCATION_KEYS", "_extract_param", "parse_interface_params", "parse_occupied_ips", "parse_dump", "_split_conf", "_build_conf", "_peer_block", "_SEP", "_params_lock", "_params_cache", "_params_cached_at", "PARAMS_TTL_SECONDS", "iface_of", "conf_path", "invalidate_server_params", "read_server_params", "read_occupied_ips", "detect_topology", "gen_keypair", "pubkey_of", "apply_config", "_backup_conf", "_restore_conf", "add_peer", "remove_peer", "block_ip", "unblock_ip", "blocked_ips", "is_blocked", "_LEGACY_SSH_CHAIN", "gated_ifaces", "remove_legacy_ssh_gate", "show_dump", "awg_responding", "watch_root", "_boot_time_iso", "service_started_at", "restart_server", "subprocess", "config", "ensure_second_address", "drop_second_address", "with_second", "without"]
