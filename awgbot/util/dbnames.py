"""
dbnames.py — имена таблиц и колонок базы, нужные нескольким слоям: исторические
таблицы (реестр для дженерик-очистки и для части бота пакета переезда) и колонки
данных хоста, которые резерву не едут. Нижний слой: схема (infra/db), util/rsvpkg
и util/rsvjrn берут списки отсюда, чтобы util не зависел от infra.
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

# Данные хоста основы, которые резерву не едут ни репликой (rsvpkg/bot `_strip` ставит
# эти значения), ни событиями журнала (rsvjrn/classes не захватывает): локальные сети и
# подписи шлюзов — таблица → {колонка: значение в реплике}.
HOST_TRIMMED: dict[str, dict[str, object]] = {"gateways": {"lan_subnets": "", "label": "", "lan_mode": 0}}
