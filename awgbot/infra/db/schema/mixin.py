"""mixin.py — часть пакета schema (см. __init__): сторож минимума схемы копии
(schema_gap) и SchemaMixin с init_schema и служебным клиентом."""
from __future__ import annotations

import sqlite3

from awgbot.util.timeutil import now_iso as _now_iso  # единый источник времени (UTC+3)

from .ddl import SCHEMA
from .selects import SERVICE_CLIENT_NAME


# Что добавили в схему миграции версий ниже 3.2.0 (сняты): по этим признакам
# сторож восстановления отличает копию, снятую до минимума, — такую базу
# нечем довести, и бот на ней упадёт на первом же запросе.
_FLOOR_COLUMNS = (
    ("clients", "kind"), ("clients", "tg_name"), ("clients", "tg_name_at"), ("clients", "tg_username"),
    ("devices", "holder_client_id"),
    ("client_pause", "pause_balance_days"), ("client_pause_histories", "pause_balance_days"),
    ("gateways", "lan_mode"),
    ("device_traffic", "rf_rx_month"), ("device_traffic", "rf_tx_month"),
    ("traffic_monthly", "rf_rx"), ("traffic_monthly", "rf_tx"),
)


def schema_gap(path: str) -> str:
    """Чем схема базы в копии отстаёт от минимума 3.2.0: «» — ничем, иначе
    первый признак словами. Таблицы, которых в базе нет вовсе, не в счёт —
    их создаст SCHEMA; пустой или нечитаемый файл — «не база SQLite»."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return "не база SQLite"
    try:
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError:
            return "не база SQLite"
        if not tables:
            return "не база SQLite"
        for table, col in _FLOOR_COLUMNS:
            if table not in tables:
                continue
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                return f"в таблице {table} нет колонки {col}"
        if "devices" in tables:
            have = {r[1] for r in con.execute("PRAGMA table_info(devices)")}
            if "full_access_link" in have:
                return "в таблице devices осталась колонка full_access_link"
            if "is_gateway" in have and con.execute("SELECT 1 FROM devices WHERE is_gateway = 1 LIMIT 1").fetchone():
                return "шлюз помечен флагом devices.is_gateway, а не слотом"
        if "device_friend" in tables and con.execute(
                "SELECT 1 FROM device_friend WHERE friend_status = 'active' LIMIT 1").fetchone():
            return "в device_friend есть активные «друзья» прежнего образца"
        return ""
    finally:
        con.close()


class SchemaMixin:
    """Инициализация схемы. Требует ядро (_connection/_tx) и _insert_guest
    из ClientsMixin (служебный клиент)."""

    # ── Инициализация ────────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Создаёт таблицы (идемпотентно) и гарантирует служебного клиента.

        Схема нормализована (identity/подписка/квота/grace/pause разведены по
        таблицам). Боевую БД не пересоздаём. Единственный шаг доводки —
        переименование колонки подсетей слота на месте (_rename_lan_subnets,
        до SCHEMA); прочих миграций нет — минимальная поддерживаемая версия
        3.2.0 (README-bot §9a), её схема полная, всё, что доводило БД версий
        старше (гостевые колонки, счёт паузы, слоты шлюзов, режим без VPN,
        РФ-трафик), снято: хост старше проходит через 3.2.0 и получает это
        там, копия старше не разворачивается (schema_gap).
        """
        with self._tx() as cur:
            self._rename_lan_subnets(cur)
            cur.executescript(SCHEMA)
        self._ensure_service_client()

    @staticmethod
    def _rename_lan_subnets(cur) -> None:
        """3.2.x → 3.3.0: колонка `gateways.home_subnets` переименована в
        `lan_subnets` — на месте, данные те же. База 3.2.x приходит и накатом,
        и восстановлением копии; SCHEMA ниже таблицу не тронет (IF NOT
        EXISTS), поэтому переименование — до неё. Снять при минимуме 3.3.0."""
        tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "gateways" not in tables:
            return
        cols = {r[1] for r in cur.execute("PRAGMA table_info(gateways)")}
        if "home_subnets" in cols and "lan_subnets" not in cols:
            cur.execute("ALTER TABLE gateways RENAME COLUMN home_subnets TO lan_subnets")

    def _ensure_service_client(self) -> None:
        """Служебный клиент «Устройства без клиента» — ровно один, создаётся один раз.

        К нему цепляются подхваченные с сервера пиры, пока админ не привяжет их к реальному
        клиенту. У него нет tg_id/периода, is_service=1, и логика уведомлений/
        блокировок его игнорирует. Подписка/квота (1:1) создаются вместе с ним.
        """
        row = self._connection().execute(
            "SELECT id FROM clients WHERE is_service = 1 LIMIT 1"
        ).fetchone()
        if row is None:
            with self._tx() as cur:
                cur.execute(
                    """INSERT INTO clients
                       (tg_id, name, device_limit, activation_status, invite_code,
                        is_service, created_at)
                       VALUES (NULL, ?, 0, 'active', NULL, 1, ?)""",
                    (SERVICE_CLIENT_NAME, _now_iso()),
                )
                cid = cur.lastrowid
                cur.execute(
                    "INSERT INTO client_subscription (client_id, period_start, "
                    "period_end, period_kind, status) VALUES (?, NULL, NULL, NULL, 'active')",
                    (cid,))
                cur.execute("INSERT INTO client_quota (client_id) VALUES (?)", (cid,))
