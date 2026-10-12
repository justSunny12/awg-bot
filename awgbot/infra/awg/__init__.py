"""
awg/ — единственный слой взаимодействия с сервером AmneziaWG.

Части: common — подавление самозаписи, замок правки конфига, AwgError,
валидация; seam — шов контейнер/хост (`_run`, `_exec*`), чтение и запись
файлов; parsers — чистые парсеры conf и dump; params — живые параметры
сервера, топология, ключи; peers — применение конфига, добавление и удаление
пира; blocks — блокировки iptables и снятие прежних ворот SSH; server —
статистика, статус контейнера и сервиса, перезапуск.

Способ доступа к awg — прямо хост (дефолт) или контейнер Amnezia прежней
схемы — переключается config.AWG_RUNTIME в одной функции `_exec`, а не
разбросан по вызовам. ВСЕ вызовы docker exec живут здесь и больше нигде.
То, что имеет смысл только у контейнера (docker inspect), закрыто
`_docker_only` и в host-режиме падает громко, а не деградирует молча.

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
from .seam import _run, in_container, HostModeUnsupported, _docker_only, _exec, _exec_i, _exec_sh, read_file, write_file
from .parsers import _OBFUSCATION_KEYS, _extract_param, parse_interface_params, parse_occupied_ips, parse_dump, _split_conf, _build_conf, _peer_block
from .params import _SEP, _params_lock, _params_cache, _params_cached_at, PARAMS_TTL_SECONDS, iface_of, conf_path, invalidate_server_params, read_server_params, read_occupied_ips, detect_topology, gen_keypair, pubkey_of
from .peers import apply_config, _backup_conf, _restore_conf, add_peer, remove_peer
from .blocks import block_ip, unblock_ip, blocked_ips, is_blocked, _LEGACY_SSH_CHAIN, gated_ifaces, remove_legacy_ssh_gate
from .server import show_dump, _inspect, container_pid, container_started_at, awg_responding, watch_root, _boot_time_iso, service_started_at, restart_server

__all__ = ["log", "_writing", "mutation_lock", "writing", "is_writing", "last_self_write", "AwgError", "_RE_PUBKEY", "_RE_IP", "_validate_key", "_validate_ip", "_run", "in_container", "HostModeUnsupported", "_docker_only", "_exec", "_exec_i", "_exec_sh", "read_file", "write_file", "_OBFUSCATION_KEYS", "_extract_param", "parse_interface_params", "parse_occupied_ips", "parse_dump", "_split_conf", "_build_conf", "_peer_block", "_SEP", "_params_lock", "_params_cache", "_params_cached_at", "PARAMS_TTL_SECONDS", "iface_of", "conf_path", "invalidate_server_params", "read_server_params", "read_occupied_ips", "detect_topology", "gen_keypair", "pubkey_of", "apply_config", "_backup_conf", "_restore_conf", "add_peer", "remove_peer", "block_ip", "unblock_ip", "blocked_ips", "is_blocked", "_LEGACY_SSH_CHAIN", "gated_ifaces", "remove_legacy_ssh_gate", "show_dump", "_inspect", "container_pid", "container_started_at", "awg_responding", "watch_root", "_boot_time_iso", "service_started_at", "restart_server", "subprocess", "config"]
