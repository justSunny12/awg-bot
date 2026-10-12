"""common.py — часть пакета migration (см. __init__): журнал, ключи состояния,
итоги включения и прогресса, тип отказа механики."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("awgbot.migration")

# Состояние живёт в server_state одним ключом. Значения намеренно строковые и
# читаемые: в базу заглядывают руками, и «running» понятнее единицы.
# Константы — в infra/db/core.py: фильтру видимости устройств состояние нужно прямо
# в запросе, а infra не может тянуть domain.
from awgbot.infra.db import MIGRATION_STATE_KEY as _STATE_KEY  # noqa: E402
from awgbot.infra.db import MIGRATION_RUNNING as STATE_RUNNING  # noqa: E402
STATE_OFF = ""

# Живым считается устройство, чей хендшейк не старше этого срока. Одно правило
# вместо пяти: неактивированные, бесустройственные и никогда не подключавшиеся
# отсекаются им же — у них хендшейка нет вовсе.
LIVE_DAYS = 14


@dataclass
class MigrationStart:
    """Итог включения рычага."""
    born: int = 0                 # сколько двойников рождено этим заходом
    already: int = 0              # сколько уже было (повторное включение)
    cohort_devices: int = 0
    cohort_clients: int = 0
    failed: list[str] = field(default_factory=list)
    # Рычаг реально включился? False — ни одного двойника не существует и ни
    # один не родился: типично «awg1 ещё не поднят». Включаться в этом состоянии
    # значило бы молча фоллбэчить всю выдачу на старые конфиги при включённом
    # рычаге.
    started: bool = True


@dataclass
class MigrationProgress:
    """Готовность переезда. Считается по ЗАМОРОЖЕННОЙ когорте."""
    clients_done: int = 0
    clients_total: int = 0
    devices_done: int = 0
    devices_total: int = 0

    @property
    def complete(self) -> bool:
        # Пустая когорта — это завершённость, а не «ещё не начали»: живых пиров
        # не было вовсе, переезжать некому.
        return self.devices_done >= self.devices_total



class ServiceErrorMigration(Exception):
    """Отказ механики переезда. Отдельный тип, чтобы не утонуть в общих
    except ServiceError у вызывающих: здесь отказ означает «не начали», и
    молча продолжать нельзя."""


__all__ = ["log", "_STATE_KEY", "STATE_RUNNING", "STATE_OFF", "LIVE_DAYS",
           "MigrationStart", "MigrationProgress", "ServiceErrorMigration"]
