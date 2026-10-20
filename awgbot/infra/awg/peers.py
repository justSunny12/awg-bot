"""peers.py — часть пакета awg (см. __init__): применение конфига (syncconf),
бэкап/откат conf, добавление и удаление пира."""
from __future__ import annotations

from typing import Optional

from awgbot.core import config
from awgbot.infra import awg as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде

from .common import AwgError


# ─────────────────────────────────────────────────────────────────────────────
# Применение конфига (syncconf, без рестарта — не рвёт активных)
# ─────────────────────────────────────────────────────────────────────────────

def apply_config(iface: Optional[str] = None) -> None:
    """awg syncconf <if> <(awg-quick strip <if>.conf) — на живую.
    Через временный файл (не process substitution) → работает в любом shell.
    Предупреждение 'world accessible' от strip идёт в stderr и безвредно.

    Файл кладём РЯДОМ С КОНФИГОМ и с маской 077, а не в /tmp с фиксированным
    именем. В нём приватный ключ сервера, и редирект создал бы его с обычной
    маской — читаемым всеми: ключ ложился бы в общий /tmp на каждое добавление
    устройства. Предсказуемое имя там же открывает подмену симлинком: редирект
    от root записал бы куда указано.
    Каталог AWG_DIR — 700 и наш.
    """
    name = pkg.iface_of(iface)
    script = (
        f'umask 077; tmp=$(mktemp "{config.AWG_DIR}/.strip.XXXXXX") || exit 1; '
        f'awg-quick strip "{pkg.conf_path(name)}" > "$tmp" 2>/dev/null && '
        f'awg syncconf "{name}" "$tmp"; '
        'rc=$?; rm -f "$tmp"; exit $rc'
    )
    pkg._exec_sh(script)


def _backup_conf(iface: Optional[str] = None) -> None:
    path = pkg.conf_path(iface)
    pkg._exec(["cp", path, path + ".bak"])


def _restore_conf(iface: Optional[str] = None) -> None:
    path = pkg.conf_path(iface)
    pkg._exec(["cp", path + ".bak", path])


# ─────────────────────────────────────────────────────────────────────────────
# Добавление / удаление пира (awg0.conf + syncconf, с откатом на .bak)
# ─────────────────────────────────────────────────────────────────────────────

def add_peer(public_key: str, psk: str, ip: str, iface: Optional[str] = None) -> None:
    """Добавляет [Peer] в conf интерфейса и применяет. Идемпотентно (если pubkey
    уже есть — не дублирует). При ошибке применения восстанавливает .bak и
    поднимает AwgError — сервер остаётся консистентным, откат БД делает services."""
    pkg._validate_key(public_key)
    pkg._validate_key(psk)
    pkg._validate_ip(ip)
    path = pkg.conf_path(iface)

    with pkg.mutation_lock:
        conf = pkg.read_file(path)
        header, peers = pkg._split_conf(conf)
        if any(p["pubkey"] == public_key for p in peers):
            return                                # уже есть — нечего делать
        peers.append(pkg._peer_block(public_key, psk, ip))
        new_conf = pkg._build_conf(header, peers)

        with pkg.writing():
            pkg._backup_conf(iface)
            pkg.write_file(path, new_conf)
            try:
                pkg.apply_config(iface)
            except AwgError:
                pkg._restore_conf(iface)
                pkg.apply_config(iface)               # вернуть демон к прежнему состоянию
                raise


def remove_peer(public_key: str, iface: Optional[str] = None) -> None:
    """Убирает [Peer] с данным pubkey и применяет. Идемпотентно. Откат как в
    add_peer.

    Интерфейс здесь не формальность: во время переезда старый пир живёт в conf
    СТАРОГО интерфейса, и удаление по дефолтному пути оставило бы его работать —
    призрачный доступ у устройства, которое человек считает удалённым."""
    pkg._validate_key(public_key)
    path = pkg.conf_path(iface)
    with pkg.mutation_lock:
        conf = pkg.read_file(path)
        header, peers = pkg._split_conf(conf)
        new_peers = [p for p in peers if p["pubkey"] != public_key]
        if len(new_peers) == len(peers):
            return                                # не было — нечего делать
        new_conf = pkg._build_conf(header, new_peers)

        with pkg.writing():
            pkg._backup_conf(iface)
            pkg.write_file(path, new_conf)
            try:
                pkg.apply_config(iface)
            except AwgError:
                pkg._restore_conf(iface)
                pkg.apply_config(iface)
                raise
