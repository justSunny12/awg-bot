"""base.py — журнал, замок перестроек, ошибки, запуск команд на хосте и в контейнере, имена наборов, интерфейс активного линка."""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Optional

from awgbot.core import config
from awgbot.infra import awg

log = logging.getLogger("awgbot.routing")

# Сериализация перестроек: реконсиляция из планировщика и правка списка из
# хендлера могут прийти одновременно, а пересборка цепочки — read-modify-write.
mutation_lock = threading.RLock()


class RoutingError(Exception):
    """Ошибка взаимодействия с ipset/iptables/ip."""


class RoutingUnavailable(RoutingError):
    """Инструментов или обвяза нет — фича неработоспособна. Не ошибка бизнес-
    операции: UI её прячет, планировщик пропускает, VPN работает как обычно."""


# ─────────────────────────────────────────────────────────────────────────────
# Запуск команд: хост и контейнер
# ─────────────────────────────────────────────────────────────────────────────

def _host(args: list[str], *, check: bool = True,
          input_data: Optional[bytes] = None, timeout: int = 20):
    try:
        proc = subprocess.run(args, input=input_data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError as e:
        raise RoutingUnavailable(f"Команда не найдена на хосте: {args[0]} ({e})")
    except subprocess.TimeoutExpired:
        raise RoutingError(f"Таймаут команды: {' '.join(args[:4])}...")
    if check and proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise RoutingError(f"Ошибка {' '.join(args[:4])}: {err}")
    return proc


# Проверки «есть ли правило/маршрут/набор» — только чтение: их зовут тик и
# рендер экранов, и ждать 20 с ответа netlink там нечего — дольше 5 с это уже
# «не смог посмотреть» (третье состояние), а не отказ.
_PROBE_TIMEOUT = 5


def _host_ok(args: list[str]) -> bool:
    """Код возврата 0? Для проверок существования правил (iptables -C и т.п.)."""
    return _host(args, check=False, timeout=_PROBE_TIMEOUT).returncode == 0


def _cont(args: list[str], *, check: bool = True):
    """Команда внутри контейнера. Идёт через awg._exec — тот же путь, которым
    ставятся блокировки, чтобы способ доступа к контейнеру был ровно один."""
    try:
        proc = awg._exec(args, check=False)
    except awg.AwgError as e:
        raise RoutingError(f"Контейнер недоступен: {e}")
    if check and proc.returncode != 0:
        raise RoutingError(
            f"Ошибка в контейнере {' '.join(args[:4])}: "
            f"{proc.stderr.decode(errors='replace').strip()}")
    return proc


def _cont_ok(args: list[str]) -> bool:
    return _cont(args, check=False).returncode == 0


# ─────────────────────────────────────────────────────────────────────────────
# Имена наборов
# ─────────────────────────────────────────────────────────────────────────────

def user_set(client_id: int) -> str:
    """Набор личных доменов клиента."""
    return f"{config.ROUTING_SET_USER_PREFIX}{int(client_id)}"


def src_set(client_id: int) -> str:
    """Набор адресов устройств клиента (для пер-клиентского правила)."""
    return f"{config.ROUTING_SET_SRC_PREFIX}{int(client_id)}"


def _active_if() -> str:
    """Интерфейс активного линка по умолчанию — из конфига: без слотов (одна
    машина, как до v2.24.0) это и есть единственный линк."""
    return config.ROUTING_GW_INTERFACE
