"""
db.py — слой доступа к SQLite для AWG-бота.

Принципы:
- БД — единственный источник истины. Контейнер (awg0.conf + iptables) — её проекция.
- Никакой бизнес-логики здесь: только чтение/запись. Склейка с контейнером — в services.py.
- Все даты хранятся строками ISO-8601 в UTC+3 (TZ проекта). Конвертацию в/из
  datetime делает вызывающий код через helpers из time-утилит (пока — прямые ISO-строки).

Таблицы:
  clients          — биллинговая сущность (клиент, срок, лимит, инвайт)
  devices          — устройство = AWG peer (ключи, IP, трафик, блокировка)
  traffic_samples  — последнее сырое значение rx/tx для вычисления дельт
  server_state     — key-value: детект рестарта контейнера, флаги сбросов/бэкапов
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from awgbot.util.timeutil import now_iso as _now_iso  # единый источник времени (UTC+3)
from awgbot.core import models


def _client_from_row(row) -> Optional["models.Client"]:
    """Объединённая строка (clients + подписка/квота, LEFT JOIN grace/pause) →
    доменный Client. Ленивые процессы: grace/pause материализуются только когда в
    соответствующей таблице есть строка (LEFT JOIN вернул не-NULL). Читает по
    именам колонок — работает с результатом JOIN из getter'ов."""
    if row is None:
        return None
    keys = row.keys()
    grace = None
    if "grace_used" in keys and row["grace_used"] is not None:
        if int(row["grace_used"]) or int(row["grace_pending_cut"]):
            grace = models.GraceState(used=int(row["grace_used"]),
                                      pending_cut=int(row["grace_pending_cut"]))
    pause = None
    if "pause_used_days" in keys and row["pause_used_days"] is not None:
        if row["pause_active_since"]:
            pause = models.PauseState(
                active_since=row["pause_active_since"],
                reserved_days=int(row["pause_reserved_days"]),
                used_days=int(row["pause_used_days"]),
                mode=row["pause_mode"],
                saved_end=row["pause_saved_end"],
                resume_code=(row["resume_code"] if "resume_code" in keys else None))
        elif int(row["pause_used_days"]):
            pause = models.PauseState(used_days=int(row["pause_used_days"]))
    return models.Client(
        id=int(row["id"]),
        tg_id=row["tg_id"],
        name=row["name"],
        device_limit=int(row["device_limit"]),
        block_reason=int(row["block_reason"]),
        is_service=int(row["is_service"]),
        activation_status=row["activation_status"],
        invite_code=row["invite_code"],
        created_at=row["created_at"],
        subscription=models.Subscription(
            period_start=row["period_start"], period_end=row["period_end"],
            period_kind=row["period_kind"], status=row["status"],
            notified_thresholds=row["notified_thresholds"]),
        quota=models.TrafficQuota(
            limit=int(row["traffic_limit"]), bonus_bytes=int(row["bonus_bytes"]),
            bonus_granted_month=int(row["bonus_granted_month"]),
            traffic_notified=row["traffic_notified"]),
        grace=grace, pause=pause)


def _device_from_row(row) -> Optional["models.Device"]:
    """Объединённая строка (devices + traffic, LEFT JOIN friend) → Device.
    friend материализуется только у гостевых устройств (строка device_friend есть
    ⇔ friend_status не NULL)."""
    if row is None:
        return None
    friend = None
    if "friend_status" in row.keys() and row["friend_status"]:
        friend = models.Friend(tg_id=row["friend_tg_id"], code=row["friend_code"],
                               status=row["friend_status"])
    return models.Device(
        id=int(row["id"]),
        client_id=int(row["client_id"]),
        name=row["name"],
        private_key=row["private_key"],
        public_key=row["public_key"],
        preshared_key=row["preshared_key"],
        address=row["address"],
        block_reason=int(row["block_reason"]),
        created_at=row["created_at"],
        full_access_link=(row["full_access_link"] if "full_access_link" in row.keys() else None),
        traffic=models.DeviceTraffic(
            limit=int(row["traffic_limit"]),
            rx_month=int(row["traffic_rx_month"]), tx_month=int(row["traffic_tx_month"]),
            rx_period=int(row["traffic_rx_period"]), tx_period=int(row["traffic_tx_period"]),
            last_handshake=row["last_handshake"], missing_count=int(row["missing_count"])),
        friend=friend)

# Имя служебного клиента, к которому цепляются app-устройства без владельца.
SERVICE_CLIENT_NAME = "Устройства без клиента"

# Реестр исторических таблиц для дженерик-очистки (слой 3c). У всех есть столбец
# archived_at, по которому считается ретеншн и идёт батч-DELETE.
HISTORY_TABLES = [
    "client_pause_histories", "client_grace_histories",
    "client_subscription_histories", "client_quota_histories",
    "device_friend_histories", "client_block_histories",
    "clients_histories", "devices_histories", "device_quota_histories",
    "traffic_monthly",
]

# JOIN-выборки: собирают нормализованные таблицы в одну строку для builder'ов.
# Подписка и квота — INNER (всегда есть); grace/pause/friend — LEFT (ленивые).
_CLIENT_SELECT = """
SELECT c.*, s.period_start, s.period_end, s.period_kind, s.status, s.notified_thresholds,
       q.traffic_limit, q.bonus_bytes, q.bonus_granted_month, q.traffic_notified,
       g.grace_used, g.grace_pending_cut,
       p.pause_active_since, p.pause_reserved_days, p.pause_used_days,
       p.pause_mode, p.pause_saved_end, p.resume_code
FROM clients c
JOIN client_subscription s ON s.client_id = c.id
JOIN client_quota q        ON q.client_id = c.id
LEFT JOIN client_grace g   ON g.client_id = c.id
LEFT JOIN client_pause p   ON p.client_id = c.id
"""

_DEVICE_SELECT = """
SELECT d.*, t.traffic_limit, t.traffic_rx_month, t.traffic_tx_month,
       t.traffic_rx_period, t.traffic_tx_period, t.last_handshake, t.missing_count,
       f.friend_tg_id, f.friend_code, f.friend_status
FROM devices d
JOIN device_traffic t     ON t.device_id = d.id
LEFT JOIN device_friend f ON f.device_id = d.id
"""

# ─────────────────────────────────────────────────────────────────────────────
# Схема
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
-- ── Клиент: identity + маска блокировки (горячее ядро) ──────────────────────
CREATE TABLE IF NOT EXISTS clients (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id               INTEGER UNIQUE,               -- nullable: до активации инвайта NULL
    name                TEXT    NOT NULL,
    device_limit        INTEGER NOT NULL DEFAULT 1,
    block_reason        INTEGER NOT NULL DEFAULT 0,   -- маска ClientBlock; 0 = не заблокирован
    activation_status   TEXT    NOT NULL DEFAULT 'pending',  -- pending | active
    invite_code         TEXT,                         -- гасится (NULL) после активации
    is_service          INTEGER NOT NULL DEFAULT 0,   -- 1 = служебный «Устройства без клиента»
    created_at          TEXT    NOT NULL
);

-- ── Подписка клиента (1:1, всегда есть) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS client_subscription (
    client_id           INTEGER PRIMARY KEY,
    period_start        TEXT,                         -- ISO-8601 UTC+3; NULL у служебного
    period_end          TEXT,                         -- ISO-8601 UTC+3; NULL у служебного/бессрочного
    period_kind         TEXT,                         -- day|week|month|year|never
    status              TEXT    NOT NULL DEFAULT 'active',   -- active | expired
    notified_thresholds TEXT    NOT NULL DEFAULT '',  -- CSV порогов истечения
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

-- ── Квота потребления клиента (1:1, всегда есть) ────────────────────────────
CREATE TABLE IF NOT EXISTS client_quota (
    client_id           INTEGER PRIMARY KEY,
    traffic_limit       INTEGER NOT NULL DEFAULT 0,   -- байты, тотал-потолок за месяц; 0 = безлимит
    bonus_bytes         INTEGER NOT NULL DEFAULT 0,   -- разовая доп.квота текущего месяца
    bonus_granted_month INTEGER NOT NULL DEFAULT 0,   -- 0/1: выдавали ли доп.квоту в этом месяце
    traffic_notified    TEXT    NOT NULL DEFAULT '',  -- CSV меток трафик-уведомлений, сброс 1-го числа
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

-- ── Отсрочка «на пару недель» (1:1, ЛЕНИВАЯ: строки нет ⇔ не бралась) ───────────
CREATE TABLE IF NOT EXISTS client_grace (
    client_id           INTEGER PRIMARY KEY,
    grace_used          INTEGER NOT NULL DEFAULT 0,   -- 0/1: бралась ли отсрочка в текущем периоде
    grace_pending_cut   INTEGER NOT NULL DEFAULT 0,   -- сек «долга», вычесть из следующего периода
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

-- ── Приостановка подписки (1:1, ЛЕНИВАЯ: строки нет ⇔ не на паузе и счётчик 0) ─
CREATE TABLE IF NOT EXISTS client_pause (
    client_id           INTEGER PRIMARY KEY,
    pause_active_since  TEXT,                         -- ISO входа; NULL = не на паузе (но строка может жить ради used_days)
    pause_reserved_days INTEGER NOT NULL DEFAULT 0,   -- зарезервировано дней вперёд (user/admin_fixed)
    pause_used_days     INTEGER NOT NULL DEFAULT 0,   -- израсходовано дней за период (лимит 28, только user)
    pause_mode          TEXT,                         -- user | admin_fixed | admin_open
    pause_saved_end     TEXT,                         -- снимок period_end для admin_open
    resume_code         TEXT,                         -- одноразовый код email-выхода (NULL вне паузы)
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

-- ── Устройство: identity + crypto + маска блокировки ────────────────────────
CREATE TABLE IF NOT EXISTS devices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    name                TEXT    NOT NULL,
    private_key         TEXT,                            -- nullable: NULL у app-устройств
    public_key          TEXT    NOT NULL UNIQUE,         -- = clientId в терминах awg
    preshared_key       TEXT    NOT NULL,
    address             TEXT    NOT NULL UNIQUE,         -- 10.8.1.X ; UNIQUE = аллокатор
    full_access_link    TEXT,                            -- nullable: vpn:// полного доступа (admin-устройство), храним как есть
    block_reason        INTEGER NOT NULL DEFAULT 0,      -- маска DeviceBlock; 0 = не заблокирован
    created_at          TEXT    NOT NULL,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

-- ── Счётчики потребления устройства (1:1, всегда есть) ──────────────────────
CREATE TABLE IF NOT EXISTS device_traffic (
    device_id           INTEGER PRIMARY KEY,
    traffic_limit       INTEGER NOT NULL DEFAULT 0,      -- байты, лимит устройства за месяц; 0 = безлимит
    traffic_rx_month    INTEGER NOT NULL DEFAULT 0,
    traffic_tx_month    INTEGER NOT NULL DEFAULT 0,
    traffic_rx_period   INTEGER NOT NULL DEFAULT 0,
    traffic_tx_period   INTEGER NOT NULL DEFAULT 0,
    last_handshake      INTEGER,                         -- unix; не затираем пустым
    missing_count       INTEGER NOT NULL DEFAULT 0,      -- сверок подряд без peer в конфиге
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

-- ── Гостевой доступ (1:1, ЛЕНИВАЯ: строки нет ⇔ обычное устройство хозяина) ──
CREATE TABLE IF NOT EXISTS device_friend (
    device_id           INTEGER PRIMARY KEY,
    friend_tg_id        INTEGER,                         -- Telegram друга
    friend_code         TEXT,                            -- инвайт-код; NULL после активации
    friend_status       TEXT,                            -- pending | active
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS traffic_samples (
    device_id           INTEGER PRIMARY KEY,
    last_rx             INTEGER NOT NULL,
    last_tx             INTEGER NOT NULL,
    sampled_at          TEXT    NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS server_state (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ui_state (
    chat_id             INTEGER PRIMARY KEY,
    nav_message_id      INTEGER,              -- id единственного активного нав-сообщения
    content_msg_ids     TEXT                  -- JSON-список id выданных контент-сообщений (ссылка/QR/файл)
);

CREATE INDEX IF NOT EXISTS idx_devices_client   ON devices(client_id);
CREATE INDEX IF NOT EXISTS idx_devices_pubkey   ON devices(public_key);
CREATE INDEX IF NOT EXISTS idx_clients_invite   ON clients(invite_code);
CREATE INDEX IF NOT EXISTS idx_clients_tg       ON clients(tg_id);
CREATE INDEX IF NOT EXISTS idx_friend_tg        ON device_friend(friend_tg_id);
CREATE INDEX IF NOT EXISTS idx_friend_code      ON device_friend(friend_code);

-- ═══════════════════════════════════════════════════════════════════════════
-- Исторические таблицы (аудит): закрытые эпизоды бизнес-процессов и удалённые
-- сущности. Метаполя archived_at (момент закрытия/архивации — от него считаем
-- ретеншн) и close_reason (auto|manual|admin|expired|deleted|renewed|...).
-- Схемы истории НЕ обязаны зеркалить активные: тут только поля, ценные аудиту.
-- Ретеншн + очистка батчами — в scheduler (слой 3c).
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS client_pause_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    pause_active_since  TEXT,
    pause_reserved_days INTEGER NOT NULL DEFAULT 0,
    pause_used_days     INTEGER NOT NULL DEFAULT 0,
    pause_mode          TEXT,
    pause_saved_end     TEXT,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE IF NOT EXISTS client_grace_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    grace_used          INTEGER NOT NULL DEFAULT 0,
    grace_pending_cut   INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE IF NOT EXISTS client_subscription_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    period_start        TEXT,
    period_end          TEXT,                         -- плановый конец (может ≠ archived_at)
    period_kind         TEXT,
    status              TEXT,
    archived_at         TEXT    NOT NULL,             -- фактический момент закрытия
    close_reason        TEXT
);

CREATE TABLE IF NOT EXISTS client_quota_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    traffic_limit       INTEGER NOT NULL DEFAULT 0,
    bonus_bytes         INTEGER NOT NULL DEFAULT 0,
    bonus_granted_month INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE IF NOT EXISTS device_friend_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER NOT NULL,
    client_id           INTEGER,                      -- владелец на момент архивации (для сшивки)
    friend_tg_id        INTEGER,
    friend_code         TEXT,
    friend_status       TEXT,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE IF NOT EXISTS client_block_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    block_reason        INTEGER NOT NULL,             -- снятая маска (что за биты были)
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE IF NOT EXISTS clients_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,             -- исходный id (снимок на момент удаления)
    tg_id               INTEGER,
    name                TEXT,
    device_limit        INTEGER,
    block_reason        INTEGER,
    is_service          INTEGER,
    created_at          TEXT,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE IF NOT EXISTS devices_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER NOT NULL,
    client_id           INTEGER,
    name                TEXT,
    public_key          TEXT,
    address             TEXT,
    block_reason        INTEGER,
    traffic_limit       INTEGER,                      -- лимит на момент снимка
    created_at          TEXT,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

-- Смены лимита потребления устройства (админ-событие) — снимок ДО изменения.
CREATE TABLE IF NOT EXISTS device_quota_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER NOT NULL,
    client_id           INTEGER,
    traffic_limit       INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

-- Помесячное потребление — метрика (не эпизод): снимок перед сбросом 1-го числа.
CREATE TABLE IF NOT EXISTS traffic_monthly (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER NOT NULL,
    client_id           INTEGER,
    month               TEXT    NOT NULL,             -- 'YYYY-MM' завершившегося месяца
    rx                  INTEGER NOT NULL DEFAULT 0,
    tx                  INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_h_pause_at   ON client_pause_histories(archived_at);
CREATE INDEX IF NOT EXISTS idx_h_pause_cli  ON client_pause_histories(client_id);
CREATE INDEX IF NOT EXISTS idx_h_grace_at   ON client_grace_histories(archived_at);
CREATE INDEX IF NOT EXISTS idx_h_grace_cli  ON client_grace_histories(client_id);
CREATE INDEX IF NOT EXISTS idx_h_sub_at     ON client_subscription_histories(archived_at);
CREATE INDEX IF NOT EXISTS idx_h_sub_cli    ON client_subscription_histories(client_id);
CREATE INDEX IF NOT EXISTS idx_h_quota_at   ON client_quota_histories(archived_at);
CREATE INDEX IF NOT EXISTS idx_h_quota_cli  ON client_quota_histories(client_id);
CREATE INDEX IF NOT EXISTS idx_h_friend_at  ON device_friend_histories(archived_at);
CREATE INDEX IF NOT EXISTS idx_h_friend_dev ON device_friend_histories(device_id);
CREATE INDEX IF NOT EXISTS idx_h_block_at   ON client_block_histories(archived_at);
CREATE INDEX IF NOT EXISTS idx_h_block_cli  ON client_block_histories(client_id);
CREATE INDEX IF NOT EXISTS idx_h_clients_at ON clients_histories(archived_at);
CREATE INDEX IF NOT EXISTS idx_h_devices_at ON devices_histories(archived_at);
CREATE INDEX IF NOT EXISTS idx_h_dquota_at   ON device_quota_histories(archived_at);
CREATE INDEX IF NOT EXISTS idx_h_dquota_dev  ON device_quota_histories(device_id);
CREATE INDEX IF NOT EXISTS idx_tm_at        ON traffic_monthly(archived_at);
CREATE INDEX IF NOT EXISTS idx_tm_dev_month ON traffic_monthly(device_id, month);
CREATE INDEX IF NOT EXISTS idx_tm_cli       ON traffic_monthly(client_id);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Подключение
# ─────────────────────────────────────────────────────────────────────────────

class Database:
    """Тонкая обёртка над sqlite3. Один экземпляр на процесс.

    sqlite3 в Python потокобезопасен при check_same_thread=False + отдельные
    курсоры; для нашей нагрузки (5 друзей, редкие операции) блокировок с запасом
    хватает. WAL включаем для параллельного чтения опросчиком во время записи.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        # Инициализируем соединение основного потока (для init_schema).
        self._connection()

    def _connection(self) -> sqlite3.Connection:
        """Соединение, привязанное к текущему потоку. services вызываются через
        asyncio.to_thread (пул потоков), поэтому соединение одно на все потоки
        использовать нельзя — sqlite3 не потокобезопасен на одном connection.
        WAL позволяет много читателей + одного писателя через РАЗНЫЕ соединения."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")     # иначе каскады не работают
            conn.execute("PRAGMA journal_mode = WAL")    # параллельное чтение
            # Каноничная пара к WAL: fsync только на чекпоинте, а не на каждом
            # commit. Целостность БД гарантирована при любом сбое; при потере
            # питания может пропасть только последняя транзакция (для наших
            # данных — одна 5-минутная выборка трафика, восполняется следующим
            # опросом). На VPS с медленным диском это главная экономия I/O.
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 5000")   # мс: ждать до 5 с, а не падать на locked
            conn.commit()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        """Транзакция с поддержкой вложенности: commit/rollback делает только
        внешний уровень. Внутри db.transaction() все операции — один атомарный
        commit (и один fsync вместо десятков)."""
        conn = self._connection()
        depth = getattr(self._local, "tx_depth", 0)
        self._local.tx_depth = depth + 1
        cur = conn.cursor()
        try:
            yield cur
            if depth == 0:
                conn.commit()
        except Exception:
            if depth == 0:
                conn.rollback()
            raise
        finally:
            self._local.tx_depth = depth
            cur.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Групповая транзакция: всё внутри — атомарно, один commit.
        Используется опросчиком трафика (целостность дельт + меньше fsync)."""
        with self._tx():
            yield

    # ── Инициализация ────────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Создаёт таблицы (идемпотентно) и гарантирует служебного клиента.

        Схема нормализована (identity/подписка/квота/grace/pause разведены по
        таблицам). Для БОЕВОЙ БД (не пересоздаём!) — секция аддитивных миграций:
        новые колонки доводятся ALTER'ом идемпотентно, данные не трогаются.
        """
        with self._tx() as cur:
            cur.executescript(SCHEMA)
        self._migrate_additive()
        self._ensure_service_client()

    def _migrate_additive(self) -> None:
        """Аддитивные миграции для существующей (боевой) БД: CREATE IF NOT EXISTS
        не добавляет колонок в уже существующие таблицы — доводим ALTER'ом.
        Идемпотентно: колонка уже есть → пропускаем. Данные не трогаем."""
        want = {
            "ui_state": [("content_msg_ids", "TEXT")],
            "client_pause": [("resume_code", "TEXT")],
        }
        con = self._connection()
        for table, cols in want.items():
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols:
                if col not in have:
                    with self._tx() as cur:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def _ensure_service_client(self) -> None:
        """Служебный клиент «Устройства без клиента» — ровно один, создаётся один раз.

        К нему цепляются app-устройства, пока админ не привяжет их к реальному
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

    def get_nav_message_id(self, chat_id: int):
        row = self._connection().execute(
            "SELECT nav_message_id FROM ui_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["nav_message_id"] if row else None

    def set_nav_message_id(self, chat_id: int, message_id) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO ui_state (chat_id, nav_message_id) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET nav_message_id = excluded.nav_message_id",
                (chat_id, message_id),
            )

    def add_content_msg_id(self, chat_id: int, message_id: int) -> None:
        """Запомнить id выданного контент-сообщения (ссылка/QR/файл/инструкция),
        чтобы удалить его при возврате в меню."""
        import json
        row = self._connection().execute(
            "SELECT content_msg_ids FROM ui_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        ids = json.loads(row["content_msg_ids"]) if row and row["content_msg_ids"] else []
        if message_id not in ids:
            ids.append(message_id)
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO ui_state (chat_id, content_msg_ids) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET content_msg_ids = excluded.content_msg_ids",
                (chat_id, json.dumps(ids)),
            )

    def pop_content_msg_ids(self, chat_id: int) -> list:
        """Забрать и очистить список id контент-сообщений (для удаления)."""
        import json
        row = self._connection().execute(
            "SELECT content_msg_ids FROM ui_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        ids = json.loads(row["content_msg_ids"]) if row and row["content_msg_ids"] else []
        if ids:
            with self._tx() as cur:
                cur.execute("UPDATE ui_state SET content_msg_ids = NULL WHERE chat_id = ?",
                            (chat_id,))
        return ids

    def get_service_client_id(self) -> int:
        row = self._connection().execute(
            "SELECT id FROM clients WHERE is_service = 1 LIMIT 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Служебный клиент не инициализирован — вызовите init_schema()")
        return row["id"]

    def find_client_by_resume_code(self, code: str) -> "Optional[int]":
        """id клиента с активной паузой и данным resume-кодом (или None).
        Матч только по активной паузе (pause_active_since NOT NULL) — код вне
        паузы недействителен. Точное сравнение (без LIKE), код чувствителен к
        регистру."""
        if not code:
            return None
        row = self._connection().execute(
            "SELECT client_id FROM client_pause "
            "WHERE resume_code = ? AND pause_active_since IS NOT NULL LIMIT 1",
            (code,)).fetchone()
        return row["client_id"] if row else None

    # ── Клиенты ──────────────────────────────────────────────────────────────

    def create_client(
        self,
        name: str,
        device_limit: int,
        period_start: str,
        period_end: str,
        invite_code: str,
        traffic_limit: int = 0,
        period_kind: Optional[str] = None,
    ) -> int:
        """Создаёт клиента в статусе pending (ждёт активации инвайта). tg_id пока NULL.
        Заводит сопутствующие 1:1 подписку и квоту. Возвращает id нового клиента."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO clients
                   (tg_id, name, device_limit, activation_status, invite_code,
                    is_service, created_at)
                   VALUES (NULL, ?, ?, 'pending', ?, 0, ?)""",
                (name, device_limit, invite_code, _now_iso()))
            cid = cur.lastrowid
            cur.execute(
                "INSERT INTO client_subscription (client_id, period_start, period_end, "
                "period_kind, status) VALUES (?, ?, ?, ?, 'active')",
                (cid, period_start, period_end, period_kind))
            cur.execute(
                "INSERT INTO client_quota (client_id, traffic_limit) VALUES (?, ?)",
                (cid, traffic_limit))
            return cid

    def get_client(self, client_id: int):
        return _client_from_row(self._connection().execute(
            _CLIENT_SELECT + " WHERE c.id = ?", (client_id,)).fetchone())

    def get_client_by_tg(self, tg_id: int):
        return _client_from_row(self._connection().execute(
            _CLIENT_SELECT + " WHERE c.tg_id = ?", (tg_id,)).fetchone())

    def get_client_by_invite(self, invite_code: str):
        """Ищет pending-клиента по неигашеному инвайт-коду."""
        return _client_from_row(self._connection().execute(
            _CLIENT_SELECT + " WHERE c.invite_code = ? AND c.activation_status = 'pending'",
            (invite_code,)).fetchone())

    def list_clients(self, include_service: bool = False,
                     exclude_tg: Optional[int] = None,
                     admin_first_tg: Optional[int] = None) -> list:
        q = _CLIENT_SELECT + " WHERE 1=1"
        params: list = []
        if not include_service:
            q += " AND c.is_service = 0"
        if exclude_tg is not None:
            q += " AND (c.tg_id IS NULL OR c.tg_id != ?)"
            params.append(exclude_tg)
        # admin_first_tg: профиль администратора всегда идёт первым в списке.
        if admin_first_tg is not None:
            q += " ORDER BY (c.tg_id = ?) DESC, c.is_service DESC, c.name COLLATE NOCASE"
            params.append(admin_first_tg)
        else:
            q += " ORDER BY c.is_service DESC, c.name COLLATE NOCASE"
        return [_client_from_row(r) for r in
                self._connection().execute(q, params).fetchall()]

    def activate_client(self, client_id: int, tg_id: int) -> None:
        """Привязывает tg_id к клиенту и гасит инвайт-код (одноразовость)."""
        with self._tx() as cur:
            cur.execute(
                """UPDATE clients
                   SET tg_id = ?, activation_status = 'active', invite_code = NULL
                   WHERE id = ?""",
                (tg_id, client_id),
            )

    # Карта: поле → (таблица, ключевая-колонка, ленивая-ли). Ленивые (grace/pause)
    # создаются строкой при первой записи. Идентити-поля — в clients.
    _CLIENT_FIELD_TABLE = {
        # clients
        "name": "clients", "device_limit": "clients", "tg_id": "clients",
        "activation_status": "clients", "invite_code": "clients", "block_reason": "clients",
        # client_subscription
        "period_start": "client_subscription", "period_end": "client_subscription",
        "period_kind": "client_subscription", "status": "client_subscription",
        "notified_thresholds": "client_subscription",
        # client_quota
        "traffic_limit": "client_quota", "bonus_bytes": "client_quota",
        "bonus_granted_month": "client_quota", "traffic_notified": "client_quota",
        # client_grace (ленивая)
        "grace_used": "client_grace", "grace_pending_cut": "client_grace",
        # client_pause (ленивая)
        "pause_active_since": "client_pause", "pause_reserved_days": "client_pause",
        "pause_used_days": "client_pause", "pause_mode": "client_pause",
        "pause_saved_end": "client_pause", "resume_code": "client_pause",
    }
    _CLIENT_LAZY = {"client_grace", "client_pause"}
    _CLIENT_KEY = {"clients": "id", "client_subscription": "client_id",
                   "client_quota": "client_id", "client_grace": "client_id",
                   "client_pause": "client_id"}

    # ── Типизированные врайтеры процессов (pause/grace/friend) ───────────────
    # Явная альтернатива update_client_fields(pause_*=...): принимают доменный
    # под-объект целиком, делают семантику видимой в вызывающем коде. clear_*
    # удаляют ленивую строку (технический сброс; аудит идёт отдельно archive_*).

    def save_pause(self, client_id: int, pause: "models.PauseState") -> None:
        """Upsert строки паузы из доменного объекта (ленивая 1:1)."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO client_pause
                   (client_id, pause_active_since, pause_reserved_days,
                    pause_used_days, pause_mode, pause_saved_end, resume_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(client_id) DO UPDATE SET
                     pause_active_since=excluded.pause_active_since,
                     pause_reserved_days=excluded.pause_reserved_days,
                     pause_used_days=excluded.pause_used_days,
                     pause_mode=excluded.pause_mode,
                     pause_saved_end=excluded.pause_saved_end,
                     resume_code=excluded.resume_code""",
                (client_id, pause.active_since, pause.reserved_days,
                 pause.used_days, str(pause.mode) if pause.mode else None,
                 pause.saved_end, pause.resume_code))

    def update_client_fields(self, client_id: int, **fields) -> None:
        """Точечное обновление полей клиента с маршрутизацией по нормализованным
        таблицам. Ленивые таблицы (grace/pause) создаются строкой при первой
        записи (INSERT OR IGNORE), затем UPDATE. Всё — в одной транзакции."""
        # сгруппировать поля по таблицам
        by_table: dict[str, dict] = {}
        for k, v in fields.items():
            table = self._CLIENT_FIELD_TABLE.get(k)
            if table is None:
                # неизвестное поле — почти всегда опечатка вызывающего; молча
                # проглотить = тихо потерять запись. Падаем громко.
                raise ValueError(f"update_client_fields: неизвестное поле {k!r}")
            by_table.setdefault(table, {})[k] = v
        if not by_table:
            return
        with self._tx() as cur:
            for table, cols in by_table.items():
                key = self._CLIENT_KEY[table]
                if table in self._CLIENT_LAZY:
                    # гарантировать строку ленивой таблицы
                    cur.execute(
                        f"INSERT OR IGNORE INTO {table} ({key}) VALUES (?)", (client_id,))
                assignments = ", ".join(f"{c} = ?" for c in cols)
                values = list(cols.values()) + [client_id]
                cur.execute(f"UPDATE {table} SET {assignments} WHERE {key} = ?", values)

    def delete_client(self, client_id: int, archive_reason: str = "deleted") -> None:
        """Удаляет клиента. Устройства уносятся каскадом (ON DELETE CASCADE).
        Служебного клиента удалять нельзя. Перед удалением — снимки в историю
        (клиент + все его устройства + их friend-эпизоды), если archive_reason
        задан (None у технических откатов)."""
        row = self.get_client(client_id)
        if row and row.is_service:
            raise ValueError("Нельзя удалить служебного клиента")
        with self._tx() as cur:
            if archive_reason:
                for dr in cur.execute("SELECT id FROM devices WHERE client_id = ?",
                                      (client_id,)).fetchall():
                    self.archive_friend(dr["id"], archive_reason, cur)
                    self.archive_device_snapshot(dr["id"], archive_reason, cur)
                # закрыть/снять все эпизоды клиента на момент смерти — иначе
                # CASCADE унесёт их без следа в аудите
                self.archive_subscription(client_id, archive_reason, cur)
                self.archive_quota(client_id, archive_reason, cur)
                self.archive_pause(client_id, archive_reason, cur)
                self.archive_grace(client_id, archive_reason, cur)
                self.archive_client_snapshot(client_id, archive_reason, cur)
            cur.execute("DELETE FROM clients WHERE id = ?", (client_id,))

    # ── Пороги уведомлений (запечатаны: наружу — множество int) ───────────────

    def get_notified(self, client_id: int) -> set[int]:
        """Возвращает множество уже отправленных порогов (в днях/условных единицах).

        Внутреннее представление — CSV-строка; наружу отдаём set[int], чтобы
        остальной код не знал про сериализацию. Если однажды заменим на отдельную
        таблицу — меняются только get_notified/add_notified/reset_notified.
        """
        row = self.get_client(client_id)
        if not row or not row.notified_thresholds:
            return set()
        return {int(x) for x in row.notified_thresholds.split(",") if x.strip()}

    def add_notified(self, client_id: int, threshold: int) -> None:
        """Добавляет порог в множество отправленных (идемпотентно)."""
        current = self.get_notified(client_id)
        if threshold in current:
            return
        current.add(threshold)
        # Храним отсортированно для читабельности файла БД при отладке.
        csv = ",".join(str(x) for x in sorted(current))
        self.update_client_fields(client_id, notified_thresholds=csv)

    def reset_notified(self, client_id: int) -> None:
        """Обнуляет отправленные пороги (вызывается при создании нового периода)."""
        self.update_client_fields(client_id, notified_thresholds="")

    def get_traffic_notified(self, client_id: int) -> set[str]:
        """Множество уже отправленных трафик-уведомлений (строковые метки:
        'cli80','cli_over','bonus','dev80:{id}','dev_over:{id}'). Сбрасывается
        1-го числа вместе с накоплением. CSV внутри — set[str] наружу."""
        row = self.get_client(client_id)
        if not row or not row.traffic_notified:
            return set()
        return {x for x in row.traffic_notified.split(",") if x.strip()}

    def add_traffic_notified(self, client_id: int, marker: str) -> None:
        """Помечает трафик-уведомление отправленным (идемпотентно)."""
        cur = self.get_traffic_notified(client_id)
        if marker in cur:
            return
        cur.add(marker)
        self.update_client_fields(client_id, traffic_notified=",".join(sorted(cur)))

    def reset_traffic_notified(self, client_id: int) -> None:
        """Сброс трафик-меток (1-го числа, новый месяц)."""
        self.update_client_fields(client_id, traffic_notified="")

    # ── Устройства ───────────────────────────────────────────────────────────

    def create_device(
        self,
        client_id: int,
        name: str,
        public_key: str,
        preshared_key: str,
        address: str,
        private_key: Optional[str] = None,
        traffic_limit: int = 0,
    ) -> int:
        """Создаёт устройство. Без private_key — «чужой» app-пир (ключа у нас нет).
        Заводит сопутствующую 1:1 строку счётчиков. Возвращает id устройства."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO devices
                   (client_id, name, private_key, public_key,
                    preshared_key, address, block_reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                (client_id, name, private_key, public_key,
                 preshared_key, address, _now_iso()))
            did = cur.lastrowid
            cur.execute(
                "INSERT INTO device_traffic (device_id, traffic_limit) VALUES (?, ?)",
                (did, traffic_limit))
            return did

    def get_device(self, device_id: int):
        return _device_from_row(self._connection().execute(
            _DEVICE_SELECT + " WHERE d.id = ?", (device_id,)).fetchone())

    def get_device_by_friend_code(self, code: str):
        return _device_from_row(self._connection().execute(
            _DEVICE_SELECT + " WHERE f.friend_code = ?", (code,)).fetchone())

    def get_device_by_friend_tg(self, tg_id: int):
        """Активное гостевое устройство, которым управляет этот Telegram-друг."""
        return _device_from_row(self._connection().execute(
            _DEVICE_SELECT + " WHERE f.friend_tg_id = ? AND f.friend_status = 'active'",
            (tg_id,)).fetchone())

    def get_devices_by_friend_tg(self, tg_id: int) -> list:
        """ВСЕ активные гостевые устройства этого друга (мультидружба: один tg_id
        может управлять несколькими устройствами разных клиентов)."""
        return [_device_from_row(r) for r in self._connection().execute(
            _DEVICE_SELECT + " WHERE f.friend_tg_id = ? AND f.friend_status = 'active' "
            "ORDER BY d.id", (tg_id,)).fetchall()]

    def set_device_friend(self, device_id: int, *, friend_tg_id=None,
                          friend_code=None, friend_status=None) -> None:
        """Точечно обновляет запись друга на устройстве. Ленивая 1:1: если все три
        поля пусты — строку device_friend удаляем (устройство перестало быть
        гостевым); иначе upsert. None-значения записываются как есть."""
        with self._tx() as cur:
            if friend_tg_id is None and friend_code is None and friend_status is None:
                cur.execute("DELETE FROM device_friend WHERE device_id = ?", (device_id,))
            else:
                cur.execute(
                    "INSERT INTO device_friend (device_id, friend_tg_id, friend_code, "
                    "friend_status) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(device_id) DO UPDATE SET "
                    "friend_tg_id=excluded.friend_tg_id, friend_code=excluded.friend_code, "
                    "friend_status=excluded.friend_status",
                    (device_id, friend_tg_id, friend_code, friend_status))

    def list_devices(self, client_id: int) -> list:
        return [_device_from_row(r) for r in self._connection().execute(
            _DEVICE_SELECT + " WHERE d.client_id = ? "
            "ORDER BY (d.full_access_link IS NOT NULL), d.created_at",
            (client_id,)).fetchall()]

    def list_all_devices(self) -> list:
        return [_device_from_row(r) for r in
                self._connection().execute(_DEVICE_SELECT).fetchall()]

    def count_devices(self, client_id: int) -> int:
        return self._connection().execute(
            "SELECT COUNT(*) AS c FROM devices WHERE client_id = ?", (client_id,)
        ).fetchone()["c"]

    def admin_device_addresses(self, admin_tg_id: int) -> list[str]:
        """Адреса (10.8.1.X) всех устройств, чей владелец — админ (по tg_id).
        Источник вайтлиста для пер-пирного SSH-к-хосту (reconcile_ssh_access).
        Служебный клиент («устройства без профиля») исключён явно: сегодня у него
        нет tg_id, но SSH-вайтлист не должен зависеть от этого неявно."""
        rows = self._connection().execute(
            "SELECT d.address FROM devices d "
            "JOIN clients c ON c.id = d.client_id "
            "WHERE c.tg_id = ? AND c.is_service = 0", (admin_tg_id,)).fetchall()
        return [r["address"] for r in rows]

    _DEVICE_FIELD_TABLE = {
        "name": "devices", "private_key": "devices",
        "block_reason": "devices", "client_id": "devices",
        "full_access_link": "devices",
        "traffic_limit": "device_traffic", "traffic_rx_month": "device_traffic",
        "traffic_tx_month": "device_traffic", "traffic_rx_period": "device_traffic",
        "traffic_tx_period": "device_traffic", "last_handshake": "device_traffic",
        "missing_count": "device_traffic",
    }
    _DEVICE_KEY = {"devices": "id", "device_traffic": "device_id"}

    def update_device_fields(self, device_id: int, **fields) -> None:
        """Точечное обновление полей устройства с маршрутизацией: identity/crypto →
        devices, счётчики/лимит → device_traffic. Одна транзакция."""
        by_table: dict[str, dict] = {}
        for k, v in fields.items():
            table = self._DEVICE_FIELD_TABLE.get(k)
            if table is None:
                raise ValueError(f"update_device_fields: неизвестное поле {k!r}")
            by_table.setdefault(table, {})[k] = v
        if not by_table:
            return
        with self._tx() as cur:
            for table, cols in by_table.items():
                key = self._DEVICE_KEY[table]
                assignments = ", ".join(f"{c} = ?" for c in cols)
                values = list(cols.values()) + [device_id]
                cur.execute(f"UPDATE {table} SET {assignments} WHERE {key} = ?", values)

    def delete_device(self, device_id: int, archive_reason: str = "deleted") -> None:
        """Удаляет устройство. Перед удалением — снимок в историю + закрытие
        friend-эпизода, если archive_reason задан (None у технических откатов)."""
        with self._tx() as cur:
            if archive_reason:
                self.archive_friend(device_id, archive_reason, cur)
                self.archive_device_snapshot(device_id, archive_reason, cur)
            cur.execute("DELETE FROM devices WHERE id = ?", (device_id,))

    def reassign_device(self, device_id: int, new_client_id: int) -> None:
        """Привязка app-устройства к реальному клиенту."""
        self.update_device_fields(device_id, client_id=new_client_id)

    # ── Трафик: накопление и сброс ───────────────────────────────────────────

    def add_traffic(self, device_id: int, d_rx: int, d_tx: int) -> None:
        """Прибавляет дельту к обоим счётчикам (месяц + период) сразу."""
        with self._tx() as cur:
            cur.execute(
                """UPDATE device_traffic SET
                     traffic_rx_month  = traffic_rx_month  + ?,
                     traffic_tx_month  = traffic_tx_month  + ?,
                     traffic_rx_period = traffic_rx_period + ?,
                     traffic_tx_period = traffic_tx_period + ?
                   WHERE device_id = ?""",
                (d_rx, d_tx, d_rx, d_tx, device_id),
            )

    def reset_month_traffic_all(self) -> None:
        """Сброс месячных счётчиков у всех устройств (1-го числа 00:00 UTC+3)."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE device_traffic SET traffic_rx_month = 0, traffic_tx_month = 0"
            )

    def reset_period_traffic(self, client_id: int) -> None:
        """Сброс ПЕРИОДНЫХ счётчиков устройств клиента (при новом периоде).
        Месячные НЕ трогаем — у них свой цикл."""
        with self._tx() as cur:
            cur.execute(
                """UPDATE device_traffic SET traffic_rx_period = 0, traffic_tx_period = 0
                   WHERE device_id IN (SELECT id FROM devices WHERE client_id = ?)""",
                (client_id,),
            )

    def get_client_traffic(self, client_id: int) -> dict[str, int]:
        """Суммарный трафик клиента по устройствам (месяц + период)."""
        row = self._connection().execute(
            """SELECT
                 COALESCE(SUM(t.traffic_rx_month), 0)  AS rx_month,
                 COALESCE(SUM(t.traffic_tx_month), 0)  AS tx_month,
                 COALESCE(SUM(t.traffic_rx_period), 0) AS rx_period,
                 COALESCE(SUM(t.traffic_tx_period), 0) AS tx_period
               FROM device_traffic t JOIN devices d ON d.id = t.device_id
               WHERE d.client_id = ?""",
            (client_id,),
        ).fetchone()
        return dict(row)

    def get_total_month_traffic(self) -> dict[str, int]:
        """Суммарное месячное потребление по ВСЕМ устройствам (для админ-панели)."""
        row = self._connection().execute(
            """SELECT COALESCE(SUM(traffic_rx_month), 0) AS rx,
                      COALESCE(SUM(traffic_tx_month), 0) AS tx
               FROM device_traffic"""
        ).fetchone()
        return dict(row)

    # ── Архивация в историю (аудит) ──────────────────────────────────────────
    # Явные типизированные методы: статичный SQL, читают активную строку → пишут
    # в *_histories с метаполями archived_at/close_reason → удаляют активную (для
    # эпизодов). Всё в переданном курсоре — вызывающий оборачивает в транзакцию,
    # чтобы переезд INSERT→DELETE был атомарным. reason — причина закрытия.

    def snapshot_pause(self, client_id: int, reason: str, cur=None) -> None:
        """Снять СНИМОК текущего эпизода паузы в историю, НЕ удаляя активную строку
        (used_days — свойство периода, живёт в client_pause до сброса периода).
        Вызывается при выходе из паузы: эпизод в аудит, счётчик остаётся."""
        if cur is None:
            with self._tx() as c:
                return self.snapshot_pause(client_id, reason, c)

        row = cur.execute("SELECT * FROM client_pause WHERE client_id = ?",
                          (client_id,)).fetchone()
        if row is None or not row["pause_active_since"]:
            return
        cur.execute(
            """INSERT INTO client_pause_histories
               (client_id, pause_active_since, pause_reserved_days, pause_used_days,
                pause_mode, pause_saved_end, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (client_id, row["pause_active_since"], row["pause_reserved_days"],
             row["pause_used_days"], row["pause_mode"], row["pause_saved_end"],
             _now_iso(), reason))

    def archive_pause(self, client_id: int, reason: str, cur=None) -> None:
        """Полностью закрыть паузу: снимок эпизода → историю + удалить активную
        строку (сброс used_days). Вызывается при СБРОСЕ ПЕРИОДА (счётчик обнуляется)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_pause(client_id, reason, c)
        self.snapshot_pause(client_id, reason, cur)
        cur.execute("DELETE FROM client_pause WHERE client_id = ?", (client_id,))

    def archive_grace(self, client_id: int, reason: str, cur=None) -> None:
        """Закрыть эпизод отсрочки: перенести строку client_grace → историю."""
        if cur is None:
            with self._tx() as c:
                return self.archive_grace(client_id, reason, c)

        row = cur.execute("SELECT * FROM client_grace WHERE client_id = ?",
                           (client_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO client_grace_histories
               (client_id, grace_used, grace_pending_cut, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?)""",
            (client_id, row["grace_used"], row["grace_pending_cut"], _now_iso(), reason))
        cur.execute("DELETE FROM client_grace WHERE client_id = ?", (client_id,))

    def archive_subscription(self, client_id: int, reason: str, cur=None) -> None:
        """Снять СНИМОК текущей подписки в историю (строку client_subscription НЕ
        удаляем — подписка 1:1 всегда есть, меняется на новый период на месте)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_subscription(client_id, reason, c)

        row = cur.execute("SELECT * FROM client_subscription WHERE client_id = ?",
                          (client_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO client_subscription_histories
               (client_id, period_start, period_end, period_kind, status,
                archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (client_id, row["period_start"], row["period_end"], row["period_kind"],
             row["status"], _now_iso(), reason))

    def archive_quota(self, client_id: int, reason: str, cur=None) -> None:
        """Снять СНИМОК текущей квоты в историю (строку client_quota НЕ удаляем)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_quota(client_id, reason, c)

        row = cur.execute("SELECT * FROM client_quota WHERE client_id = ?",
                          (client_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO client_quota_histories
               (client_id, traffic_limit, bonus_bytes, bonus_granted_month,
                archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (client_id, row["traffic_limit"], row["bonus_bytes"],
             row["bonus_granted_month"], _now_iso(), reason))

    def archive_device_quota(self, device_id: int, reason: str, cur=None) -> None:
        """Снять СНИМОК текущего лимита устройства в историю (перед изменением).
        Активную строку device_traffic не трогаем."""
        if cur is None:
            with self._tx() as c:
                return self.archive_device_quota(device_id, reason, c)
        row = cur.execute(
            "SELECT t.traffic_limit, d.client_id FROM device_traffic t "
            "JOIN devices d ON d.id = t.device_id WHERE t.device_id = ?",
            (device_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO device_quota_histories
               (device_id, client_id, traffic_limit, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?)""",
            (device_id, row["client_id"], row["traffic_limit"], _now_iso(), reason))

    def archive_friend(self, device_id: int, reason: str, cur=None) -> None:
        """Закрыть гостевой доступ: перенести строку device_friend → историю.
        client_id владельца берём из devices для сшивки."""
        if cur is None:
            with self._tx() as c:
                return self.archive_friend(device_id, reason, c)

        row = cur.execute("SELECT * FROM device_friend WHERE device_id = ?",
                          (device_id,)).fetchone()
        if row is None:
            return
        owner = cur.execute("SELECT client_id FROM devices WHERE id = ?",
                            (device_id,)).fetchone()
        cur.execute(
            """INSERT INTO device_friend_histories
               (device_id, client_id, friend_tg_id, friend_code, friend_status,
                archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (device_id, owner["client_id"] if owner else None, row["friend_tg_id"],
             row["friend_code"], row["friend_status"], _now_iso(), reason))
        cur.execute("DELETE FROM device_friend WHERE device_id = ?", (device_id,))

    def archive_block(self, client_id: int, mask: int, reason: str, cur=None) -> None:
        """Записать снятый эпизод блокировки клиента (какая маска была снята)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_block(client_id, mask, reason, c)

        if not mask:
            return
        cur.execute(
            """INSERT INTO client_block_histories
               (client_id, block_reason, archived_at, close_reason)
               VALUES (?, ?, ?, ?)""",
            (client_id, int(mask), _now_iso(), reason))

    def archive_client_snapshot(self, client_id: int, reason: str, cur=None) -> None:
        """Снимок клиента перед удалением (identity на момент смерти)."""
        if cur is None:
            with self._tx() as c:
                return self.archive_client_snapshot(client_id, reason, c)

        row = cur.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO clients_histories
               (client_id, tg_id, name, device_limit, block_reason, is_service,
                created_at, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], row["tg_id"], row["name"], row["device_limit"],
             row["block_reason"], row["is_service"], row["created_at"],
             _now_iso(), reason))

    def archive_device_snapshot(self, device_id: int, reason: str, cur=None) -> None:
        """Снимок устройства перед удалением."""
        if cur is None:
            with self._tx() as c:
                return self.archive_device_snapshot(device_id, reason, c)

        row = cur.execute(
            "SELECT d.*, t.traffic_limit AS t_limit FROM devices d "
            "JOIN device_traffic t ON t.device_id = d.id WHERE d.id = ?",
            (device_id,)).fetchone()
        if row is None:
            return
        cur.execute(
            """INSERT INTO devices_histories
               (device_id, client_id, name, public_key, address,
                block_reason, traffic_limit, created_at, archived_at, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["id"], row["client_id"], row["name"],
             row["public_key"], row["address"], row["block_reason"],
             row["t_limit"], row["created_at"], _now_iso(), reason))

    def snapshot_monthly_traffic(self, month: str, cur=None) -> None:
        """Снять помесячный снимок потребления ВСЕХ устройств (перед сбросом 1-го
        числа). month = 'YYYY-MM' завершившегося месяца. Метрика, не эпизод —
        активные счётчики не трогаем (их обнуляет reset_month_traffic_all)."""
        if cur is None:
            with self._tx() as c:
                return self.snapshot_monthly_traffic(month, c)

        rows = cur.execute(
            "SELECT t.device_id, d.client_id, t.traffic_rx_month, t.traffic_tx_month "
            "FROM device_traffic t JOIN devices d ON d.id = t.device_id").fetchall()
        stamp = _now_iso()
        for r in rows:
            if not r["traffic_rx_month"] and not r["traffic_tx_month"]:
                continue          # нулевые месяцы не пишем
            cur.execute(
                """INSERT INTO traffic_monthly
                   (device_id, client_id, month, rx, tx, archived_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (r["device_id"], r["client_id"], month,
                 r["traffic_rx_month"], r["traffic_tx_month"], stamp))

    def purge_histories(self, cutoff_iso: str, batch_size: int = 500) -> dict[str, int]:
        """Удалить исторические записи старше cutoff (по archived_at) из ВСЕХ
        таблиц реестра HISTORY_TABLES. Дженерик: имена берутся из своего реестра
        (не из пользовательского ввода), SQL по archived_at универсален и
        безопасен. Удаляем БАТЧАМИ (LIMIT в цикле), чтобы не держать долгую
        блокировку на большой истории. Каждый батч — отдельная короткая транзакция.
        Возвращает {таблица: удалено_строк}."""
        removed: dict[str, int] = {}
        for table in HISTORY_TABLES:
            total = 0
            while True:
                with self._tx() as cur:
                    cur.execute(
                        f"DELETE FROM {table} WHERE rowid IN ("
                        f"  SELECT rowid FROM {table} WHERE archived_at < ? LIMIT ?)",
                        (cutoff_iso, batch_size))
                    n = cur.rowcount
                total += n
                if n < batch_size:
                    break
            if total:
                removed[table] = total
        return removed

    # ── traffic_samples: база для дельт ──────────────────────────────────────

    def get_sample(self, device_id: int) -> Optional[sqlite3.Row]:
        return self._connection().execute(
            "SELECT * FROM traffic_samples WHERE device_id = ?", (device_id,)
        ).fetchone()

    def set_sample(self, device_id: int, last_rx: int, last_tx: int) -> None:
        """Запоминает последнее сырое значение rx/tx как базу для следующей дельты."""
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO traffic_samples (device_id, last_rx, last_tx, sampled_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                     last_rx = excluded.last_rx,
                     last_tx = excluded.last_tx,
                     sampled_at = excluded.sampled_at""",
                (device_id, last_rx, last_tx, _now_iso()),
            )

    # ── server_state: key-value ──────────────────────────────────────────────

    def get_state(self, key: str) -> Optional[str]:
        row = self._connection().execute(
            "SELECT value FROM server_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO server_state (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (key, value),
            )

    # ── Аллокация IP ─────────────────────────────────────────────────────────

    def allocate_ip(
        self,
        subnet_prefix: str = "10.8.1",
        occupied_extra: Optional[set[str]] = None,
        start_host: int = 1,
        end_host: int = 254,
    ) -> str:
        """Возвращает первый свободный адрес вида {subnet_prefix}.N.

        Занятые адреса берём из БД И из occupied_extra (адреса из живого awg0.conf,
        чтобы учесть app-устройства, которых может ещё не быть в БД). Это важно:
        приложение и бот делят один пул и одну логику «первый свободный», поэтому
        источником занятости должен быть реальный конфиг, а не только БД.

        start_host=1 потому что в докерной Amnezia сервер занимает .0, клиенты — с .1.
        """
        occupied: set[str] = {
            r["address"] for r in self._connection().execute("SELECT address FROM devices")
        }
        if occupied_extra:
            occupied |= occupied_extra
        for n in range(start_host, end_host + 1):
            candidate = f"{subnet_prefix}.{n}"
            if candidate not in occupied:
                return candidate
        raise RuntimeError("Пул IP исчерпан")


__all__ = ["Database", "SERVICE_CLIENT_NAME"]
