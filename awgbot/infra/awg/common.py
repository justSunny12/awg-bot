"""common.py — часть пакета awg (см. __init__): подавление самозаписи, замок
правки конфига, AwgError, валидация значений для shell/конфига."""
from __future__ import annotations

import logging
import re
import threading
import time as _time
from contextlib import contextmanager

log = logging.getLogger("awgbot.awg")

# ─────────────────────────────────────────────────────────────────────────────
# Подавление самозаписи: пока бот сам правит файлы Amnezia, вотчдог игнорирует
# события (иначе реконсиляция сработала бы на нашу же запись). После выхода из
# контекста фиксируем метку времени — страховочная mtime-сетка вотчдога
# использует её, чтобы не среагировать на хвост нашей записи.
# ─────────────────────────────────────────────────────────────────────────────

_writing = threading.Event()
_last_self_write: float = 0.0

# Сериализация read-modify-write конфига: без него два параллельных
# add_peer/remove_peer теряют изменения друг друга (последняя запись побеждает).
mutation_lock = threading.RLock()


@contextmanager
def writing():
    """Контекст «бот сейчас пишет файлы» — вотчдог проверяет is_writing()."""
    global _last_self_write
    _writing.set()
    try:
        yield
    finally:
        _last_self_write = _time.time()
        _writing.clear()


def is_writing() -> bool:
    return _writing.is_set()


def last_self_write() -> float:
    """Unix-время окончания последней собственной записи (для mtime-сетки)."""
    return _last_self_write

# ─────────────────────────────────────────────────────────────────────────────
# Исключения
# ─────────────────────────────────────────────────────────────────────────────

class AwgError(Exception):
    """Общая ошибка взаимодействия с сервером AmneziaWG."""


# ─────────────────────────────────────────────────────────────────────────────
# Валидация значений, попадающих в shell/конфиг
# ─────────────────────────────────────────────────────────────────────────────

_RE_PUBKEY = re.compile(r"^[A-Za-z0-9+/]{43}=$")          # base64 32 байта
_RE_IP = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _validate_key(key: str) -> str:
    if not _RE_PUBKEY.match(key):
        raise AwgError(f"Некорректный ключ: {key!r}")
    return key


def _validate_ip(ip: str) -> str:
    if not _RE_IP.match(ip):
        raise AwgError(f"Некорректный IP: {ip!r}")
    octets = ip.split(".")
    if any(not (0 <= int(o) <= 255) for o in octets):
        raise AwgError(f"Некорректный IP: {ip!r}")
    return ip
