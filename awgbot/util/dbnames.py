"""
dbnames.py — имена исторических таблиц базы (реестр для дженерик-очистки и
для части бота пакета переезда). Нижний слой: схема (infra/db) и util/rsvpkg
берут список отсюда, чтобы util не зависел от infra.
"""
from __future__ import annotations

# У всех есть столбец archived_at, по которому считается ретеншн и идёт батч-DELETE.
HISTORY_TABLES = [
    "client_pause_histories", "client_grace_histories",
    "client_subscription_histories", "client_quota_histories",
    "device_friend_histories", "client_block_histories",
    "clients_histories", "devices_histories", "device_quota_histories",
    "traffic_monthly", "server_traffic_monthly",
]
