"""schema.py — схема SQLite, JOIN-выборки, конвертеры строк и миграции.

Здесь всё, что описывает ФОРМУ данных: DDL (SCHEMA), выборки, собирающие
нормализованные таблицы в одну строку, builder'ы доменных моделей из строк и
SchemaMixin с init_schema и миграциями. Порядок вызовов в init_schema —
контракт совместимости с минимальной поддерживаемой версией.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

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
        balance = int(row["pause_balance_days"] or 0)
        if row["pause_active_since"]:
            pause = models.PauseState(
                active_since=row["pause_active_since"],
                reserved_days=int(row["pause_reserved_days"]),
                used_days=int(row["pause_used_days"]),
                balance_days=balance,
                mode=row["pause_mode"],
                saved_end=row["pause_saved_end"],
                resume_code=(row["resume_code"] if "resume_code" in keys else None))
        elif int(row["pause_used_days"]) or balance:
            pause = models.PauseState(used_days=int(row["pause_used_days"]), balance_days=balance)
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
        kind=(row["kind"] if "kind" in keys else "owner"),
        tg_name=(row["tg_name"] if "tg_name" in keys else "") or "",
        tg_name_at=(row["tg_name_at"] if "tg_name_at" in keys else None),
        tg_username=(row["tg_username"] if "tg_username" in keys else "") or "",
        routing_allowed=(int(row["routing_allowed"]) if "routing_allowed" in keys else 0),
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
    """Строка _DEVICE_SELECT (devices + traffic, LEFT JOIN friend) → Device.
    Все выборки устройств идут через этот SELECT, поэтому колонки здесь читаются
    без оглядки на их наличие. friend материализуется только у гостевых устройств
    (строка device_friend есть ⇔ friend_status не NULL)."""
    if row is None:
        return None
    friend = None
    if row["friend_status"]:
        friend = models.Friend(code=row["friend_code"], status=row["friend_status"])
    return models.Device(
        id=int(row["id"]),
        client_id=int(row["client_id"]),
        name=row["name"],
        private_key=row["private_key"],
        public_key=row["public_key"],
        preshared_key=row["preshared_key"],
        address=row["address"],
        block_reason=int(row["block_reason"]),
        routing_on=int(row["routing_on"]),
        iface=row["iface"] or "",
        twin_of=(int(row["twin_of"]) if row["twin_of"] is not None else None),
        is_gateway=int(row["is_gateway_eff"]),
        created_at=row["created_at"],
        traffic=models.DeviceTraffic(
            limit=int(row["traffic_limit"]),
            rx_month=int(row["traffic_rx_month"]), tx_month=int(row["traffic_tx_month"]),
            rx_period=int(row["traffic_rx_period"]), tx_period=int(row["traffic_tx_period"]),
            last_handshake=row["last_handshake"], missing_count=int(row["missing_count"]),
            rf_rx_month=int(row["rf_rx_month"] or 0), rf_tx_month=int(row["rf_tx_month"] or 0)),
        friend=friend,
        holder_client_id=(int(row["holder_client_id"]) if row["holder_client_id"] is not None else None),
        holder_tg_id=row["holder_tg_id"], holder_name=row["holder_name"] or "",
        holder_tg_name=row["holder_tg_name"] or "", holder_tg_username=row["holder_tg_username"] or "",
        owner_tg_id=row["owner_tg_id"], owner_name=row["owner_name"] or "",
        owner_tg_name=row["owner_tg_name"] or "", owner_tg_username=row["owner_tg_username"] or "")


def _link_conf_params(link_if: str) -> tuple[int, str]:
    """(ListenPort, /30 линка) из живого конфига интерфейса на ВПС; конфига
    нет (тесты, свежая машина) — умолчания первого слота из config."""
    import re
    from pathlib import Path
    from awgbot.core import config
    port, cidr = config.ROUTING_LINK_PORTS[0], config.ROUTING_LINK_CIDRS[0]
    try:
        text = Path(config.AWG_DIR, f"{link_if}.conf").read_text(encoding="utf-8")
    except OSError:
        return port, cidr
    m = re.search(r"(?m)^\s*ListenPort\s*=\s*(\d+)", text)
    if m:
        port = int(m.group(1))
    m = re.search(r"(?m)^\s*Address\s*=\s*([\d.]+)/(\d+)", text)
    if m:
        import ipaddress
        try:
            cidr = str(ipaddress.ip_interface(f"{m.group(1)}/{m.group(2)}").network)
        except ValueError:
            pass
    return port, cidr


def _gateway_from_row(row) -> Optional["models.Gateway"]:
    if row is None:
        return None
    return models.Gateway(
        id=int(row["id"]), device_id=int(row["device_id"]), link_if=row["link_if"],
        link_port=int(row["link_port"]), link_cidr=row["link_cidr"],
        preferred=int(row["preferred"]),
        home_subnets=[n for n in (row["home_subnets"] or "").split() if n],
        label=row["label"] or "", created_at=row["created_at"] or "",
        lan_mode=int(row["lan_mode"] or 0) if "lan_mode" in row.keys() else 0)

# Имя служебного клиента, к которому цепляются пиры без владельца (карантин).
SERVICE_CLIENT_NAME = "Устройства без клиента"

# Реестр исторических таблиц для дженерик-очистки (слой 3c). У всех есть столбец
# archived_at, по которому считается ретеншн и идёт батч-DELETE.
HISTORY_TABLES = [
    "client_pause_histories", "client_grace_histories",
    "client_subscription_histories", "client_quota_histories",
    "device_friend_histories", "client_block_histories",
    "clients_histories", "devices_histories", "device_quota_histories",
    "traffic_monthly", "server_traffic_monthly",
]

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
    created_at          TEXT    NOT NULL,
    -- Тип профиля: owner — со своей подпиской; guest —
    -- гость без подписки, держит переданные ему устройства одного владельца.
    kind                TEXT    NOT NULL DEFAULT 'owner',
    -- Имя Telegram-аккаунта (first + last name) — для ссылок на человека в
    -- текстах; обновляет middleware по каждому сообщению, пусто — ещё не писал.
    tg_name             TEXT    NOT NULL DEFAULT '',
    tg_name_at          TEXT,                         -- когда имя обновлялось; NULL — никогда
    tg_username         TEXT    NOT NULL DEFAULT '',  -- публичный @username, если есть: ссылка t.me/ работает у всех
    -- Условная маршрутизация. Здесь только
    -- РАЗРЕШЕНИЕ админа; само «включено» живёт пер-девайсно (devices.routing_on),
    -- а состояние профиля выводится из него. Снятие разрешения гасит эффект, но
    -- флаги устройств НЕ стирает: вернул разрешение — настройка восстановилась.
    routing_allowed     INTEGER NOT NULL DEFAULT 0    -- 0/1: админ разрешил фичу клиенту
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
    pause_used_days     INTEGER NOT NULL DEFAULT 0,   -- израсходовано дней за период (только user)
    -- Счёт дней паузы (v2.22.0): годовая — 28 за период, ежемесячная — +2 за
    -- каждое своевременное продление (накопление до 12 таких). Вход в паузу
    -- списывает резерв, досрочный выход возвращает неиспользованное.
    pause_balance_days  INTEGER NOT NULL DEFAULT 0,
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
    private_key         TEXT,                            -- nullable: NULL у пиров, подхваченных с сервера (карантин)
    public_key          TEXT    NOT NULL UNIQUE,         -- = clientId в терминах awg
    preshared_key       TEXT    NOT NULL,
    address             TEXT    NOT NULL UNIQUE,         -- 10.8.1.X ; UNIQUE = аллокатор
    block_reason        INTEGER NOT NULL DEFAULT 0,      -- маска DeviceBlock; 0 = не заблокирован
    routing_on          INTEGER NOT NULL DEFAULT 0,      -- 0/1: условная маршрутизация НА ЭТОМ устройстве
    -- Интерфейс, на котором живёт пир. ПУСТАЯ строка = интерфейс по умолчанию
    -- (config.AWG_INTERFACE), а не «неизвестно»: так миграция БД обходится без
    -- бэкфилла, а после переезда значение нормализуется обратно в пустое.
    iface               TEXT    NOT NULL DEFAULT '',
    is_gateway          INTEGER NOT NULL DEFAULT 0,      -- УСТАРЕЛО (v2.24.0): слоты шлюзов — таблица gateways; колонка пуста
    -- id старой строки у двойника, рождённого переездом. NULL = обычное
    -- устройство. По имени пару не собрать: name не уникален и его правят
    -- прямо в окне переезда.
    twin_of             INTEGER,
    created_at          TEXT    NOT NULL,
    -- Держатель: кто управляет устройством через бота,
    -- если не владелец. Слот, квота, подписка — у владельца (client_id).
    -- NULL = своё устройство.
    holder_client_id    INTEGER,
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (holder_client_id) REFERENCES clients(id) ON DELETE SET NULL
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

-- ── Приглашение на устройство (1:1, ЛЕНИВАЯ: строки нет ⇔ приглашения нет) ──
-- Только ОЖИДАЮЩИЙ код: после активации устройство получает держателя
-- (devices.holder_client_id), а строка удаляется. friend_tg_id остался от
-- прежней модели «друг = tg_id на устройстве», не используется.
CREATE TABLE IF NOT EXISTS device_friend (
    device_id           INTEGER PRIMARY KEY,
    friend_tg_id        INTEGER,
    friend_code         TEXT,                            -- инвайт-код
    friend_status       TEXT,                            -- pending
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

-- ── Личный список доменов условной маршрутизации (1:N, ЛЕНИВАЯ) ─────────────
-- Строк нет ⇔ список пуст. Базовую часть (национальные зоны) сюда НЕ пишем:
-- она статична, живёт в conf и добавляется генератором dnsmasq-конфига поверх.
-- PK (client_id, domain) даёт дедуп даром и он же — индекс выборки по клиенту.
CREATE TABLE IF NOT EXISTS client_routing_domains (
    client_id           INTEGER NOT NULL,
    domain              TEXT    NOT NULL,             -- нормализованный (нижний регистр, без схемы/www)
    added_at            TEXT    NOT NULL,
    PRIMARY KEY (client_id, domain),
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

-- ── Когорта переезда профилей (docs/ROADMAP.md, п.3) ────────────────────────
-- Живые устройства НА МОМЕНТ включения рычага. Замораживается намеренно: считай
-- по живому правилу постоянно — и знаменатель гуляет, а «миграция завершена»
-- становится состоянием, которое умеет расзавершаться (молчавший три недели пир
-- проснулся, и 12/12 превратилось в 12/13).
--
-- Хранится СТРОКАМИ, а не пересчитывается: пересчёт после рестарта бота собрал
-- бы когорту заново по свежим хендшейкам, и заморозка была бы фиктивной.
CREATE TABLE IF NOT EXISTS migration_cohort (
    device_id           INTEGER PRIMARY KEY,             -- id СТАРОГО устройства
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS traffic_samples (
    device_id           INTEGER PRIMARY KEY,
    last_rx             INTEGER NOT NULL,
    last_tx             INTEGER NOT NULL,
    last_update         TEXT    NOT NULL,             -- когда счётчики последний раз менялись
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);
-- учёт РФ-трафика: базы дельт по счётчикам nft
-- устройства — отдельно от traffic_samples: у тех строку рождает опрос awg,
-- и чужая «первая база» с нулями сделала бы всё потребление пира одной дельтой
CREATE TABLE IF NOT EXISTS rf_samples (
    device_id           INTEGER PRIMARY KEY,
    last_up             INTEGER NOT NULL,
    last_dn             INTEGER NOT NULL,
    last_update         TEXT    NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);
-- архив итога РФ-трафика сервера за месяц (по устройствам — traffic_monthly.rf_*)
CREATE TABLE IF NOT EXISTS server_traffic_monthly (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    month               TEXT    NOT NULL,
    rf_rx               INTEGER NOT NULL DEFAULT 0,
    rf_tx               INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_devices_holder   ON devices(holder_client_id) WHERE holder_client_id IS NOT NULL;
-- public_key и tg_id уже UNIQUE — у них есть автоиндекс, отдельные дублировали бы его
CREATE INDEX IF NOT EXISTS idx_clients_invite   ON clients(invite_code);
-- пары переезда: twins_by_origin и подзапрос видимости ходят по twin_of;
-- частичный индекс пуст вне окна переезда
CREATE INDEX IF NOT EXISTS idx_devices_twin     ON devices(twin_of) WHERE twin_of IS NOT NULL;
-- прежний индекс единственности шлюза; колонка пуста с v2.24.0, индекс безвреден
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_gateway ON devices(is_gateway) WHERE is_gateway = 1;

-- ── Слоты шлюзов условной маршрутизации ─────────
-- Устройство админа на машине-шлюзе и её линк на ВПС. Один слот несёт
-- трафик, остальные в резерве; кто именно — в state (routing_active_gateway).
CREATE TABLE IF NOT EXISTS gateways (
    id            INTEGER PRIMARY KEY,                   -- номер слота: 1, 2 …
    device_id     INTEGER NOT NULL UNIQUE,               -- аплинк малины — устройство админа
    link_if       TEXT    NOT NULL UNIQUE,               -- awglink, awglink2
    link_port     INTEGER NOT NULL UNIQUE,               -- 443, 8443
    link_cidr     TEXT    NOT NULL UNIQUE,               -- 10.99.99.0/30
    preferred     INTEGER NOT NULL DEFAULT 0,            -- 0/1: берёт трафик при холодном старте
    home_subnets  TEXT    NOT NULL DEFAULT '',           -- локальные подсети за шлюзом, через пробел
    label         TEXT    NOT NULL DEFAULT '',           -- подпись места («дом 1»)
    lan_mode      INTEGER NOT NULL DEFAULT 0,            -- 0/1: «за шлюзом — без VPN»
    created_at    TEXT    NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);
-- предпочтительный — не более одного: держит БД, как прежде держала единственность шлюза
CREATE UNIQUE INDEX IF NOT EXISTS idx_gateways_preferred ON gateways(preferred) WHERE preferred = 1;
-- истечение: предфильтр «активные с конечным периодом до границы»
CREATE INDEX IF NOT EXISTS idx_sub_end          ON client_subscription(status, period_end);
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
    pause_balance_days  INTEGER NOT NULL DEFAULT 0,
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


class SchemaMixin:
    """Инициализация схемы и миграции. Требует ядро (_connection/_tx) и
    _insert_guest из ClientsMixin."""

    # ── Инициализация ────────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Создаёт таблицы (идемпотентно) и гарантирует служебного клиента.

        Схема нормализована (identity/подписка/квота/grace/pause разведены по
        таблицам). Для БОЕВОЙ БД (не пересоздаём!) миграции идут после SCHEMA
        и идемпотентны. Их одна: минимальная поддерживаемая версия — v2.10.0
        (README-bot §9a), а её схема уже полная; всё, что доводило БД версий
        старше (колонки маршрутизации и переезда, переименование sampled_at,
        флаг шлюза, схлопывание режимов личных списков…), снято — хост старше
        проходит через v2.10.0 и получает это там.
        """
        self._migrate_guest_role_columns()
        self._migrate_pause_balance_column()
        with self._tx() as cur:
            cur.executescript(SCHEMA)
        self._migrate_drop_full_access()
        self._ensure_service_client()
        self._migrate_friends_to_guests()
        self._migrate_gateway_slots()
        self._migrate_gateway_lan_mode()
        self._migrate_rf_traffic()

    def _migrate_gateway_slots(self) -> None:
        """v2.24.0: флаг devices.is_gateway → строка
        в gateways (слот 1, предпочтительный, линк из конфига). Порт и /30
        линка — из живого конфига интерфейса, без него — умолчания слота 1.
        Ключи состояния бандла получают суффикс слота. Идемпотентно: второй
        проход не находит флага."""
        from awgbot.core import config
        con = self._connection()
        row = con.execute("SELECT id FROM devices WHERE is_gateway = 1").fetchone()
        if row is None:
            return
        link_if = config.ROUTING_GW_INTERFACE or "awglink"
        port, cidr = _link_conf_params(link_if)
        with self._tx() as cur:
            have = cur.execute("SELECT 1 FROM gateways WHERE id = 1").fetchone()
            if have is None:
                cur.execute(
                    "INSERT INTO gateways(id, device_id, link_if, link_port, link_cidr, preferred, "
                    "home_subnets, label, created_at) VALUES (1, ?, ?, ?, ?, 1, ?, '', ?)",
                    (int(row["id"]), link_if, port, cidr,
                     " ".join(config.ROUTING_HOME_SUBNETS), _now_iso()))
                for old, new in (("gw_bundle_issued_at", "gw_bundle_issued_at_1"),
                                 ("gw_bundle_ssh_allow", "gw_bundle_ssh_allow_1"),
                                 ("gw_bundle_ssh_allow_notified", "gw_bundle_ssh_allow_notified_1")):
                    v = cur.execute("SELECT value FROM server_state WHERE key = ?", (old,)).fetchone()
                    if v is not None:
                        cur.execute("INSERT OR REPLACE INTO server_state(key, value) VALUES (?, ?)",
                                    (new, v["value"]))
                        cur.execute("DELETE FROM server_state WHERE key = ?", (old,))
                cur.execute("INSERT OR REPLACE INTO server_state(key, value) VALUES ('routing_active_gateway', '1')")
            cur.execute("UPDATE devices SET is_gateway = 0 WHERE is_gateway = 1")

    def _migrate_gateway_lan_mode(self) -> None:
        """v3.0.0: gateways.lan_mode — «за шлюзом — без
        VPN» по слоту. Идемпотентно."""
        con = self._connection()
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "gateways" not in tables:
            return
        have = {r["name"] for r in con.execute("PRAGMA table_info(gateways)")}
        if "lan_mode" not in have:
            with self._tx() as cur:
                cur.execute("ALTER TABLE gateways ADD COLUMN lan_mode INTEGER NOT NULL DEFAULT 0")

    def _migrate_rf_traffic(self) -> None:
        """v3.1.0: device_traffic.rf_rx_month/rf_tx_month и
        traffic_monthly.rf_rx/rf_tx — РФ-часть потребления. Идемпотентно."""
        con = self._connection()
        for table, cols in (("device_traffic", ("rf_rx_month", "rf_tx_month")),
                            ("traffic_monthly", ("rf_rx", "rf_tx"))):
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            missing = [c for c in cols if c not in have]
            if missing:
                with self._tx() as cur:
                    for c in missing:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {c} INTEGER NOT NULL DEFAULT 0")

    def _migrate_guest_role_columns(self) -> None:
        """v2.20.0: clients.kind и devices.holder_client_id.
        CREATE TABLE IF NOT EXISTS существующие таблицы не доводит — колонки
        добавляются здесь, ДО SCHEMA (индекс по holder_client_id в SCHEMA
        требует колонку). Идемпотентно."""
        con = self._connection()
        tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "clients" in tables:
            have = {r["name"] for r in con.execute("PRAGMA table_info(clients)")}
            if "kind" not in have:
                with self._tx() as cur:
                    cur.execute("ALTER TABLE clients ADD COLUMN kind TEXT NOT NULL DEFAULT 'owner'")
            if "tg_name" not in have:
                with self._tx() as cur:
                    cur.execute("ALTER TABLE clients ADD COLUMN tg_name TEXT NOT NULL DEFAULT ''")
            if "tg_name_at" not in have:
                with self._tx() as cur:
                    cur.execute("ALTER TABLE clients ADD COLUMN tg_name_at TEXT")
            if "tg_username" not in have:
                with self._tx() as cur:
                    cur.execute("ALTER TABLE clients ADD COLUMN tg_username TEXT NOT NULL DEFAULT ''")
                    # имена уже свежие — стартовый проход их не тронул бы;
                    # юзернеймы нужны сразу у всех, поэтому «состарить» разово
                    cur.execute("UPDATE clients SET tg_name_at = NULL")
        if "devices" in tables:
            have = {r["name"] for r in con.execute("PRAGMA table_info(devices)")}
            if "holder_client_id" not in have:
                with self._tx() as cur:
                    cur.execute("ALTER TABLE devices ADD COLUMN holder_client_id INTEGER "
                                "REFERENCES clients(id) ON DELETE SET NULL")

    def _migrate_pause_balance_column(self) -> None:
        """v2.22.0: счёт дней паузы (client_pause.pause_balance_days и та же
        колонка в истории). Идемпотентно. Сами балансы для действующих профилей
        считает services.migrate_pause_balances — разово, по истории продлений."""
        con = self._connection()
        for table in ("client_pause", "client_pause_histories"):
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (table,)).fetchone()
            if exists is None:
                continue
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if "pause_balance_days" not in have:
                with self._tx() as cur:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN pause_balance_days "
                                "INTEGER NOT NULL DEFAULT 0")

    def _migrate_friends_to_guests(self) -> None:
        """v2.20.0: прежние «друзья» (device_friend.status = active, tg на
        устройстве) становятся гостевыми профилями-держателями. Имя — «Друг» до
        первого сообщения (middleware подтянет из Telegram). Друг, который уже
        обычный клиент, держит устройство своим профилем. Идемпотентно: строк
        active после переноса не остаётся."""
        con = self._connection()
        rows = con.execute(
            "SELECT device_id, friend_tg_id FROM device_friend "
            "WHERE friend_status = 'active' AND friend_tg_id IS NOT NULL").fetchall()
        if not rows:
            return
        with self._tx() as cur:
            for r in rows:
                tg = int(r["friend_tg_id"])
                holder = cur.execute("SELECT id FROM clients WHERE tg_id = ?", (tg,)).fetchone()
                hid = int(holder["id"]) if holder is not None else self._insert_guest(cur, tg, "Друг")
                cur.execute("UPDATE devices SET holder_client_id = ? WHERE id = ?",
                            (hid, int(r["device_id"])))
                cur.execute("DELETE FROM device_friend WHERE device_id = ?", (int(r["device_id"]),))

    def _migrate_drop_full_access(self) -> None:
        """Разовая зачистка: колонка devices.full_access_link осталась от
        вырезанной фичи «устройство полного доступа».

        В ней лежала зашифрованная vpn://-ссылка с root-доступом к хосту. Фичи
        больше нет, а секрет — есть: он продолжал бы ездить в каждом бэкапе,
        уходящем в Telegram. Поэтому сначала затираем значение, и только потом
        пробуем убрать саму колонку. Порядок именно такой: DROP COLUMN появился
        в SQLite 3.35 и может не пройти (старый sqlite, вьюха поверх таблицы) —
        тогда секрет всё равно уже стёрт, а лишняя пустая колонка безвредна.
        Идемпотентно: колонки нет → выходим сразу."""
        con = self._connection()
        have = {r["name"] for r in con.execute("PRAGMA table_info(devices)")}
        if "full_access_link" not in have:
            return
        with self._tx() as cur:
            cur.execute("UPDATE devices SET full_access_link = NULL "
                        "WHERE full_access_link IS NOT NULL")
        try:
            with self._tx() as cur:
                cur.execute("ALTER TABLE devices DROP COLUMN full_access_link")
        except sqlite3.OperationalError:
            pass                    # колонка осталась пустой — это безвредно

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
