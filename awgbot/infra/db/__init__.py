"""
infra.db — слой доступа к SQLite для AWG-бота.

Принципы:
- БД — единственный источник истины. Сервер (awg0.conf + правила блокировок) —
  её проекция.
- Никакой бизнес-логики здесь: только чтение/запись. Склейка с сервером — в
  domain/services/.
- Все даты хранятся строками ISO-8601 в TZ проекта. Конвертацию в/из datetime
  делает вызывающий код через helpers из timeutil.

Основные таблицы (полная схема — SCHEMA в schema.py):
  clients          — биллинговая сущность (клиент, срок, лимит, инвайт)
  devices          — устройство = AWG peer (ключи, IP, трафик, блокировка)
  traffic_samples  — последнее сырое значение rx/tx для вычисления дельт
  server_state     — key-value: метка старта сервера, флаги сбросов/бэкапов,
                     горячие счётчики
"""

from __future__ import annotations

import logging

from .schema import (SCHEMA, SERVICE_CLIENT_NAME, HISTORY_TABLES, _CLIENT_SELECT,
                     _DEVICE_SELECT, _client_from_row, _device_from_row, SchemaMixin)
from .core import DatabaseCore, MIGRATION_STATE_KEY, MIGRATION_RUNNING
from .clients import ClientsMixin
from .devices import DevicesMixin
from .traffic import TrafficMixin
from .history import HistoryMixin
from .routing import RoutingMixin
from .nav import NavMixin
from .broadcast import BroadcastMixin

log = logging.getLogger(__name__)


class Database(SchemaMixin, ClientsMixin, DevicesMixin, TrafficMixin, HistoryMixin,
               RoutingMixin, NavMixin, BroadcastMixin, DatabaseCore):
    """Тонкая обёртка над sqlite3. Один экземпляр на процесс.

    sqlite3 в Python потокобезопасен при check_same_thread=False + отдельные
    курсоры; для нашей нагрузки (5 друзей, редкие операции) блокировок с запасом
    хватает. WAL включаем для параллельного чтения опросчиком во время записи.
    """


__all__ = ["Database", "SERVICE_CLIENT_NAME", "SCHEMA", "HISTORY_TABLES",
           "MIGRATION_STATE_KEY", "MIGRATION_RUNNING",
           "_CLIENT_SELECT", "_DEVICE_SELECT", "_client_from_row", "_device_from_row"]
