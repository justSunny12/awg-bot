"""schema/ — схема SQLite, JOIN-выборки, конвертеры строк.

Части: rows — builder'ы доменных моделей из строк и параметры линка; selects —
имя служебного клиента, реестр исторических таблиц, JOIN-выборки; ddl — DDL
(SCHEMA); mixin — SchemaMixin (схема, служебный клиент, линия базы).

Здесь всё, что описывает ФОРМУ данных: DDL (SCHEMA), выборки, собирающие
нормализованные таблицы в одну строку, builder'ы доменных моделей из строк и
SchemaMixin с init_schema. Миграций нет: минимальная поддерживаемая версия —
3.3.0, её схема полная, и хост ниже проходит через неё (README-bot §9a);
копия ниже версии совместимости (core/compat) не разворачивается.
"""

from __future__ import annotations

from .rows import _client_from_row, _device_from_row, _link_conf_params, _gateway_from_row
from .selects import SERVICE_CLIENT_NAME, HISTORY_TABLES, _CLIENT_SELECT, _DEVICE_SELECT
from .ddl import SCHEMA
from .mixin import SchemaMixin

__all__ = ["_client_from_row", "_device_from_row", "_link_conf_params", "_gateway_from_row", "SERVICE_CLIENT_NAME", "HISTORY_TABLES", "_CLIENT_SELECT", "_DEVICE_SELECT", "SCHEMA", "SchemaMixin"]
