"""selects.py — часть пакета schema (см. __init__): имя служебного пользователя,
реестр исторических таблиц, JOIN-выборки пользователей и устройств."""
from __future__ import annotations

# Имя служебного пользователя, к которому цепляются пиры без владельца (карантин).
SERVICE_CLIENT_NAME = "Устройства без пользователя"
# Прежнее имя (до 4.0.2 включительно): базы, созданные раньше, переименовываются на
# старте (SchemaMixin._ensure_service_client). Снимается вместе со сдвигом минимума
# main на выпуск, где появилось переименование.
LEGACY_SERVICE_CLIENT_NAME = "Устройства без клиента"

# Реестр исторических таблиц для дженерик-очистки (слой 3c) — util/dbnames
# (его же читает часть бота пакета переезда без зависимости от infra).
from awgbot.util.dbnames import HISTORY_TABLES  # noqa: E402 — реэкспорт схемы

__all_history__ = HISTORY_TABLES

# JOIN-выборки: собирают нормализованные таблицы в одну строку для builder'ов.
# Подписка и квота — INNER (всегда есть); grace/pause/friend — LEFT (ленивые).
_CLIENT_SELECT = """
SELECT c.*, s.period_start, s.period_end, s.period_kind, s.status, s.notified_thresholds,
       q.traffic_limit, q.bonus_bytes, q.bonus_granted_month, q.traffic_notified,
       g.grace_used, g.grace_pending_cut,
       p.pause_active_since, p.pause_reserved_days, p.pause_used_days,
       p.pause_balance_days, p.pause_mode, p.pause_saved_end, p.resume_code
FROM clients c
JOIN client_subscription s ON s.client_id = c.id
JOIN client_quota q        ON q.client_id = c.id
LEFT JOIN client_grace g   ON g.client_id = c.id
LEFT JOIN client_pause p   ON p.client_id = c.id
"""

_DEVICE_SELECT = """
SELECT d.*,
       EXISTS (SELECT 1 FROM gateways g
               WHERE g.device_id = d.id
                  OR (d.twin_of IS NOT NULL AND g.device_id = d.twin_of)) AS is_gateway_eff,
       t.traffic_limit, t.traffic_rx_month, t.traffic_tx_month,
       t.traffic_rx_period, t.traffic_tx_period, t.last_handshake, t.missing_count,
       t.rf_rx_month, t.rf_tx_month,
       f.friend_code, f.friend_status,
       h.tg_id AS holder_tg_id, h.name AS holder_name, h.tg_name AS holder_tg_name,
       h.tg_username AS holder_tg_username,
       oc.tg_id AS owner_tg_id, oc.name AS owner_name, oc.tg_name AS owner_tg_name,
       oc.tg_username AS owner_tg_username
FROM devices d
JOIN device_traffic t     ON t.device_id = d.id
LEFT JOIN device_friend f ON f.device_id = d.id
LEFT JOIN clients h       ON h.id = d.holder_client_id
LEFT JOIN clients oc      ON oc.id = d.client_id
"""
