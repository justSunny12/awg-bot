"""
services — бизнес-логика: склейка db + awg + configgen.

Пакет из миксинов по областям (blocks, clients, devices, subscription, traffic,
reconcile, firewall, gateway_link, routing, status); типы и исключения — в
types.py. Снаружи импортируют по-прежнему из awgbot.domain.services.

Слой синхронный (db и awg блокирующие). Async-слой (хендлеры, планировщик)
вызывает эти методы через asyncio.to_thread, чтобы docker exec не морозил loop.
Поэтому services НЕ шлют сообщения сами, а возвращают список Notification —
их рассылает async-слой.

Здесь живут потоки, спроектированные ранее: создание клиента с инвайтом,
активация, добавление/удаление устройства с откатом, продление с остатком,
опрос трафика, проверка сроков, реконсиляция состава пиров и блокировок,
карантин чужих пиров.
"""

from __future__ import annotations

import logging


from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.infra import awg
from awgbot.infra import email_resume
from awgbot.infra import routing
from awgbot.domain import configgen
from awgbot.domain import routing as domain_routing
from awgbot.domain.migration import MigrationMixin
from awgbot.domain.selfupdate import SelfUpdateMixin
from awgbot.domain.mailmix import MailMixin
from awgbot.domain.backupcrypto import BackupCryptoMixin
from awgbot.domain.privatedns import PrivateDnsMixin
from awgbot.core.blocks import DeviceBlock, ClientBlock, DEVICE_TRAFFIC_ANY
from awgbot.core import models
from awgbot.core.enums import SubStatus, ActivationStatus, PauseMode, PeriodKind, FriendStatus

from awgbot.domain.services.types import (
    BYTES_PER_GB, SECONDS_PER_DAY,
    ServiceError, LimitReached, Notification, ClientCreated, ActivationResult,
    RoutingAddResult, DeviceCreated, FriendActivation, GuestUpgrade, PauseCredit,
    ExtendResult, DaysExtension,
)
from awgbot.domain.services.base import ServicesBase
from awgbot.domain.services.status import StatusMixin
from awgbot.domain.services.blocks import BlocksMixin
from awgbot.domain.services.clients import ClientsMixin
from awgbot.domain.services.devices import DevicesMixin
from awgbot.domain.services.subscription import SubscriptionMixin
from awgbot.domain.services.traffic import TrafficMixin, _MONTH_CUT_MINUTES
from awgbot.domain.services.reconcile import ReconcileMixin
from awgbot.domain.services.firewall import FirewallMixin
from awgbot.domain.services.gateway_link import GatewayLinkMixin
from awgbot.domain.services.routing import RoutingMixin


log = logging.getLogger("awgbot.services")


# ─────────────────────────────────────────────────────────────────────────────
# Services
# ─────────────────────────────────────────────────────────────────────────────

class Services(ServicesBase, StatusMixin, BlocksMixin, ClientsMixin, DevicesMixin,
               SubscriptionMixin, TrafficMixin, ReconcileMixin, FirewallMixin,
               GatewayLinkMixin, RoutingMixin,
               SelfUpdateMixin, MailMixin, BackupCryptoMixin, MigrationMixin, PrivateDnsMixin):
    """Сборка из миксинов по областям: каждый — свой модуль пакета, тела
    методов те же, что были в одном файле. Порядок баз — порядок разделов
    прежнего services.py."""


__all__ = [
    "Services", "ServiceError", "LimitReached", "Notification",
    "ClientCreated", "ActivationResult", "DeviceCreated", "ExtendResult",
    # остальное, что было модульными именами services.py и осталось доступно
    "RoutingAddResult", "FriendActivation", "GuestUpgrade", "PauseCredit", "DaysExtension",
    "BYTES_PER_GB", "SECONDS_PER_DAY", "_MONTH_CUT_MINUTES", "log",
    "config", "settings", "timeutil", "awg", "email_resume", "routing", "configgen",
    "domain_routing", "models", "DeviceBlock", "ClientBlock", "DEVICE_TRAFFIC_ANY",
    "SubStatus", "ActivationStatus", "PauseMode", "PeriodKind", "FriendStatus",
    "ServicesBase", "StatusMixin", "BlocksMixin", "ClientsMixin", "DevicesMixin",
    "SubscriptionMixin", "TrafficMixin", "ReconcileMixin", "FirewallMixin",
    "GatewayLinkMixin", "RoutingMixin", "SelfUpdateMixin", "MailMixin",
    "BackupCryptoMixin", "MigrationMixin", "PrivateDnsMixin",
]
