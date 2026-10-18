-- COMPAT_FLOOR 3.4.0
-- Снимок на версию совместимости (tests/unit/test_schema_line.py): переснимается
-- только вместе со сдвигом COMPAT_FLOOR — python -m tests.unit.test_schema_line

CREATE TABLE client_block_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    block_reason        INTEGER NOT NULL,             -- снятая маска (что за биты были)
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE client_grace (
    client_id           INTEGER PRIMARY KEY,
    grace_used          INTEGER NOT NULL DEFAULT 0,   -- 0/1: бралась ли отсрочка в текущем периоде
    grace_pending_cut   INTEGER NOT NULL DEFAULT 0,   -- сек «долга», вычесть из следующего периода
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE TABLE client_grace_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    grace_used          INTEGER NOT NULL DEFAULT 0,
    grace_pending_cut   INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE client_pause (
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

CREATE TABLE client_pause_histories (
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

CREATE TABLE client_quota (
    client_id           INTEGER PRIMARY KEY,
    traffic_limit       INTEGER NOT NULL DEFAULT 0,   -- байты, тотал-потолок за месяц; 0 = безлимит
    bonus_bytes         INTEGER NOT NULL DEFAULT 0,   -- разовая доп.квота текущего месяца
    bonus_granted_month INTEGER NOT NULL DEFAULT 0,   -- 0/1: выдавали ли доп.квоту в этом месяце
    traffic_notified    TEXT    NOT NULL DEFAULT '',  -- CSV меток трафик-уведомлений, сброс 1-го числа
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE TABLE client_quota_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    traffic_limit       INTEGER NOT NULL DEFAULT 0,
    bonus_bytes         INTEGER NOT NULL DEFAULT 0,
    bonus_granted_month INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE client_routing_domains (
    client_id           INTEGER NOT NULL,
    domain              TEXT    NOT NULL,             -- нормализованный (нижний регистр, без схемы/www)
    added_at            TEXT    NOT NULL,
    PRIMARY KEY (client_id, domain),
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE TABLE client_subscription (
    client_id           INTEGER PRIMARY KEY,
    period_start        TEXT,                         -- ISO-8601 UTC+3; NULL у служебного
    period_end          TEXT,                         -- ISO-8601 UTC+3; NULL у служебного/бессрочного
    period_kind         TEXT,                         -- day|week|month|year|never
    status              TEXT    NOT NULL DEFAULT 'active',   -- active | expired
    notified_thresholds TEXT    NOT NULL DEFAULT '',  -- CSV порогов истечения
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE TABLE client_subscription_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id           INTEGER NOT NULL,
    period_start        TEXT,
    period_end          TEXT,                         -- плановый конец (может ≠ archived_at)
    period_kind         TEXT,
    status              TEXT,
    archived_at         TEXT    NOT NULL,             -- фактический момент закрытия
    close_reason        TEXT
);

CREATE TABLE clients (
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

CREATE TABLE clients_histories (
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

CREATE TABLE device_friend (
    device_id           INTEGER PRIMARY KEY,
    friend_tg_id        INTEGER,
    friend_code         TEXT,                            -- инвайт-код
    friend_status       TEXT,                            -- pending
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE device_friend_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER NOT NULL,
    client_id           INTEGER,                      -- владелец на момент архивации (для сшивки)
    friend_tg_id        INTEGER,
    friend_code         TEXT,
    friend_status       TEXT,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE device_quota_histories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER NOT NULL,
    client_id           INTEGER,
    traffic_limit       INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL,
    close_reason        TEXT
);

CREATE TABLE device_traffic (
    device_id           INTEGER PRIMARY KEY,
    traffic_limit       INTEGER NOT NULL DEFAULT 0,      -- байты, лимит устройства за месяц; 0 = безлимит
    traffic_rx_month    INTEGER NOT NULL DEFAULT 0,
    traffic_tx_month    INTEGER NOT NULL DEFAULT 0,
    traffic_rx_period   INTEGER NOT NULL DEFAULT 0,
    traffic_tx_period   INTEGER NOT NULL DEFAULT 0,
    rf_rx_month         INTEGER NOT NULL DEFAULT 0,      -- РФ-часть потребления за месяц (v3.1.0)
    rf_tx_month         INTEGER NOT NULL DEFAULT 0,
    last_handshake      INTEGER,                         -- unix; не затираем пустым
    missing_count       INTEGER NOT NULL DEFAULT 0,      -- сверок подряд без peer в конфиге
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE devices (
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

CREATE TABLE devices_histories (
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

CREATE TABLE gateways (
    id            INTEGER PRIMARY KEY,                   -- номер слота: 1, 2 …
    device_id     INTEGER NOT NULL UNIQUE,               -- аплинк шлюза — устройство админа
    link_if       TEXT    NOT NULL UNIQUE,               -- awglink, awglink2
    link_port     INTEGER NOT NULL UNIQUE,               -- 443, 8443
    link_cidr     TEXT    NOT NULL UNIQUE,               -- 10.99.99.0/30
    preferred     INTEGER NOT NULL DEFAULT 0,            -- 0/1: берёт трафик при холодном старте
    lan_subnets   TEXT    NOT NULL DEFAULT '',           -- локальные подсети за шлюзом, через пробел
    label         TEXT    NOT NULL DEFAULT '',           -- подпись места («дача»)
    lan_mode      INTEGER NOT NULL DEFAULT 0,            -- 0/1: «за шлюзом — без VPN»
    created_at    TEXT    NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE migration_cohort (
    device_id           INTEGER PRIMARY KEY,             -- id СТАРОГО устройства
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE rf_samples (
    device_id           INTEGER PRIMARY KEY,
    last_up             INTEGER NOT NULL,
    last_dn             INTEGER NOT NULL,
    last_update         TEXT    NOT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE server_state (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL
);

CREATE TABLE server_traffic_monthly (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    month               TEXT    NOT NULL,
    rf_rx               INTEGER NOT NULL DEFAULT 0,
    rf_tx               INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL
);

CREATE TABLE traffic_monthly (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER NOT NULL,
    client_id           INTEGER,
    month               TEXT    NOT NULL,             -- 'YYYY-MM' завершившегося месяца
    rx                  INTEGER NOT NULL DEFAULT 0,
    tx                  INTEGER NOT NULL DEFAULT 0,
    rf_rx               INTEGER NOT NULL DEFAULT 0,      -- РФ-часть месяца (v3.1.0)
    rf_tx               INTEGER NOT NULL DEFAULT 0,
    archived_at         TEXT    NOT NULL
);

CREATE TABLE traffic_samples (
    device_id           INTEGER PRIMARY KEY,
    last_rx             INTEGER NOT NULL,
    last_tx             INTEGER NOT NULL,
    last_update         TEXT    NOT NULL,             -- когда счётчики последний раз менялись
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);

CREATE TABLE ui_state (
    chat_id             INTEGER PRIMARY KEY,
    nav_message_id      INTEGER,              -- id единственного активного нав-сообщения
    content_msg_ids     TEXT                  -- JSON-список id выданных контент-сообщений (ссылка/QR/файл)
);
