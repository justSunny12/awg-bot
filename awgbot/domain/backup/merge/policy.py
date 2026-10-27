"""
policy.py — реестр правил слияния при возврате пользователей с резерва: у каждой
колонки каждой таблицы данных пользователей есть правило, новая колонка без
правила роняет сторож-тест (как реестр слотовых ключей).

Правила колонок (три базы: `base` — снимок, с которого резерв начал, `cur` —
живая база основы, `rep` — реплика бота основы на резерве):

- `changed`  — значение реплики, если оно отличается от снимка (резерв менял
               поле), иначе значение основы (её изменение после снимка остаётся);
- `mask`     — побитно: бит, поставленный или снятый на резерве, — как на
               резерве; прочие — как у основы;
- `counter`  — `cur + (rep − base)`; счётчик на резерве меньше снимка — был
               сброс месяца: значение реплики, а дельта основы `cur − base`
               уходит в архив прошлого месяца (`traffic_monthly`);
- `max`      — большее из двух (время последнего рукопожатия);
- `cur`      — только основы (базы счётчиков ядра её хоста, закрытые ключи
               устройств — в реплику они не едут);
- `rep`      — реплики (меню пользователей);
- `key`      — ключ строки: не сливается.

Таблицы без правил по колонкам: `TABLE_CUR` — целиком основы (слоты, пара,
базы счётчиков), `TABLE_REP` — целиком реплики, `TABLE_APPEND` — строки
реплики дописываются (история: в снимок она не ехала, всё в реплике — новое),
`TABLE_STATE` — `server_state`: только ключи белого списка пакета из реплики.
"""
from __future__ import annotations

CHANGED, MASK, COUNTER, MAX, CUR, REP, KEY = "changed", "mask", "counter", "max", "cur", "rep", "key"

# таблицы данных пользователей: ключ строки → правила колонок
TABLES: dict[str, dict] = {
    "clients": {
        "_key": ("id",), "_cascade": "client",
        "id": KEY, "tg_id": CHANGED, "name": CHANGED, "device_limit": CHANGED, "block_reason": MASK,
        "activation_status": CHANGED, "invite_code": CHANGED, "is_service": CUR, "created_at": CUR,
        "kind": CHANGED, "tg_name": CHANGED, "tg_name_at": CHANGED, "tg_username": CHANGED,
        "routing_allowed": CHANGED,
    },
    "client_subscription": {
        "_key": ("client_id",), "_parent": ("clients", "client_id"),
        "client_id": KEY, "period_start": CHANGED, "period_end": CHANGED, "period_kind": CHANGED, "status": CHANGED,
        "notified_thresholds": CHANGED,
    },
    "client_quota": {
        "_key": ("client_id",), "_parent": ("clients", "client_id"),
        "client_id": KEY, "traffic_limit": CHANGED, "bonus_bytes": CHANGED, "bonus_granted_month": CHANGED,
        "traffic_notified": CHANGED,
    },
    "client_grace": {
        "_key": ("client_id",), "_parent": ("clients", "client_id"),
        "client_id": KEY, "grace_used": CHANGED, "grace_pending_cut": CHANGED,
    },
    "client_pause": {
        "_key": ("client_id",), "_parent": ("clients", "client_id"),
        "client_id": KEY, "pause_active_since": CHANGED, "pause_reserved_days": CHANGED, "pause_used_days": CHANGED,
        "pause_balance_days": CHANGED, "pause_mode": CHANGED, "pause_saved_end": CHANGED, "resume_code": CHANGED,
    },
    "devices": {
        "_key": ("id",), "_cascade": "device", "_parent": ("clients", "client_id"),
        "id": KEY, "client_id": CHANGED, "name": CHANGED, "private_key": CUR, "public_key": CHANGED,
        "preshared_key": CHANGED, "address": CHANGED, "block_reason": MASK, "routing_on": CHANGED, "iface": CHANGED,
        "is_gateway": CUR, "twin_of": CHANGED, "created_at": CUR, "holder_client_id": CHANGED,
    },
    "device_traffic": {
        "_key": ("device_id",), "_parent": ("devices", "device_id"),
        "device_id": KEY, "traffic_limit": CHANGED, "traffic_rx_month": COUNTER, "traffic_tx_month": COUNTER,
        "traffic_rx_period": COUNTER, "traffic_tx_period": COUNTER, "rf_rx_month": COUNTER, "rf_tx_month": COUNTER,
        "last_handshake": MAX, "missing_count": CUR,
    },
    "device_friend": {
        "_key": ("device_id",), "_parent": ("devices", "device_id"),
        "device_id": KEY, "friend_tg_id": CHANGED, "friend_code": CHANGED, "friend_status": CHANGED,
    },
    "client_routing_domains": {
        "_key": ("client_id", "domain"), "_parent": ("clients", "client_id"),
        "client_id": KEY, "domain": KEY, "added_at": CHANGED,
    },
    "migration_cohort": {
        "_key": ("device_id",), "_parent": ("devices", "device_id"),
        "device_id": KEY,
    },
}

TABLE_CUR: tuple[str, ...] = ("traffic_samples", "rf_samples", "gateways", "reserve_peer", "reserve_traffic_monthly",
                               "jrn_meta")
TABLE_REP: tuple[str, ...] = ("ui_state",)
TABLE_APPEND: tuple[str, ...] = (
    "client_pause_histories", "client_grace_histories", "client_subscription_histories", "client_quota_histories",
    "device_friend_histories", "client_block_histories", "clients_histories", "devices_histories",
    "device_quota_histories", "traffic_monthly", "server_traffic_monthly",
)
TABLE_STATE: tuple[str, ...] = ("server_state",)

# уникальные значения, за которые спорят строка резерва и строка, созданная основой после снимка
UNIQUE: dict[str, tuple[str, ...]] = {"devices": ("address", "public_key"), "clients": ("tg_id", "invite_code")}
# ссылки на id при переносе строк под новый id
REFS: dict[str, list[tuple[str, str]]] = {
    "clients": [("devices", "client_id"), ("devices", "holder_client_id"), ("client_subscription", "client_id"),
                ("client_quota", "client_id"), ("client_grace", "client_id"), ("client_pause", "client_id"),
                ("client_routing_domains", "client_id")],
    "devices": [("device_traffic", "device_id"), ("device_friend", "device_id"), ("migration_cohort", "device_id"),
                ("devices", "twin_of")],
}
# месячный архив при сбросе месяца на резерве: колонки счётчиков → колонки архива
MONTH_ARCHIVE = {"traffic_rx_month": "rx", "traffic_tx_month": "tx", "rf_rx_month": "rf_rx", "rf_tx_month": "rf_tx"}


def columns(table: str) -> dict[str, str]:
    return {k: v for k, v in TABLES[table].items() if not k.startswith("_")}


def key_of(table: str) -> tuple[str, ...]:
    return tuple(TABLES[table]["_key"])


def all_tables() -> set[str]:
    return set(TABLES) | set(TABLE_CUR) | set(TABLE_REP) | set(TABLE_APPEND) | set(TABLE_STATE)
