"""
config.py — фасад конфигурации AWG-бота.

Значения вынесены в conf/*.yaml по разделам (app, subscription, limits, grace,
pause, quiet_hours, monitoring). Этот модуль их грузит и раскладывает в те же
модульного уровня имена (config.GRACE_DAYS и т.д.), которыми пользуется весь код,
— чтобы разбиение хранилища не потребовало правок в модулях-потребителях.

Секреты (BOT_TOKEN, ADMIN_ID) — только из окружения/.env, НЕ из yaml (yaml в git).
Крипто-материал сервера (обфускация, ключи, psk, порт) — не тут, его читает live
из контейнера awg.py (единый источник истины).
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Пути (FHS: код в /opt, состояние в /var/lib, конфиг/секреты в /etc)
# ─────────────────────────────────────────────────────────────────────────────
# Состояние и конфиг ВЫНЕСЕНЫ из папки кода: перемещение/обновление кода их не
# трогает, а установщик/детект работают по фиксированным путям. Для разработки
# и нестандартных инсталляций всё переопределяется переменными окружения; при их
# отсутствии берём FHS-путь, а если его нет (запуск из исходников) — локальную
# папку в КОРНЕ репозитория (config.py лежит в awgbot/core/ → корень = parents[2]).
BASE_DIR = Path(__file__).resolve().parents[2]


def _resolve_dir(env_var: str, fhs: str, dev: Path) -> Path:
    v = os.environ.get(env_var)
    if v:
        return Path(v)
    return Path(fhs) if Path(fhs).exists() else dev


# conf/*.yaml редактируются установщиком → живут в /etc (переживают обновление кода)
CONF_DIR = _resolve_dir("AWG_BOT_CONF_DIR", "/etc/awg-bot/conf", BASE_DIR / "conf")
# БД и бэкапы — состояние в /var/lib (каталог заводит установщик; для dev — ./data).
# main.py создаёт подкаталоги при старте.
DATA_DIR = _resolve_dir("AWG_BOT_DATA_DIR", "/var/lib/awg-bot", BASE_DIR / "data")
DB_PATH = DATA_DIR / "bot.db"
BACKUP_DIR = DATA_DIR / "backups"


def _load_yaml(name: str) -> dict:
    """Прочитать conf/<name>.yaml → dict (пустой при отсутствии/пустоте)."""
    path = CONF_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ─────────────────────────────────────────────────────────────────────────────
# Секреты из .env (минимальный парсер, без зависимости от dotenv)
# ─────────────────────────────────────────────────────────────────────────────
def _load_env_file(path: Path) -> None:
    """KEY=VALUE из .env, не перезатирая реальное окружение (приоритет у него —
    удобно для systemd EnvironmentFile)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# Секреты в норме приходят от systemd (EnvironmentFile=/etc/awg-bot/env → уже в
# окружении). Здесь — запасной путь для ручного запуска: грузим первый найденный
# из AWG_BOT_ENV / /etc/awg-bot/env / ./.env (реальное окружение приоритетнее —
# _load_env_file использует setdefault).
for _env_path in (os.environ.get("AWG_BOT_ENV"), "/etc/awg-bot/env", str(BASE_DIR / ".env")):
    if _env_path and Path(_env_path).exists():
        _load_env_file(Path(_env_path))
        break

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
ADMIN_ID: int = int(os.environ["ADMIN_ID"]) if os.environ.get("ADMIN_ID") else 0

# Секреты шифрования бэкапов — ТОЛЬКО из окружения/.env (в git не коммитятся).
# Ровно один режим: BACKUP_KEY (base64 32-байтного случайного ключа) ИЛИ
# BACKUP_PASSPHRASE (человеко-запоминаемая фраза, ключ выводится argon2id).
# Оба пустые → бэкапы не шифруются (старое поведение). Заполняются скриптом
# manage_secrets.py. Восстановление — restore_backup.py (на awg-хосте).
BACKUP_KEY: str = os.environ.get("BACKUP_KEY", "")
BACKUP_PASSPHRASE: str = os.environ.get("BACKUP_PASSPHRASE", "")
BACKUP_ENCRYPTION_ENABLED: bool = bool(BACKUP_KEY or BACKUP_PASSPHRASE)

# ── Email-выход из приостановки (фича «backfromvacation») ─────────────────────
# Клиент, заперевшийся в паузе (Telegram только через этот VPN), присылает
# одноразовый resume-код письмом — бот опрашивает IMAP-ящик исходяще (портов не
# открываем) и снимает паузу. Логин/пароль (app-specific password iCloud) —
# ТОЛЬКО из окружения/.env, не в git. Пусто → email-выход выключен.
EMAIL_RESUME_LOGIN: str = os.environ.get("EMAIL_RESUME_LOGIN", "")
EMAIL_RESUME_PASSWORD: str = os.environ.get("EMAIL_RESUME_PASSWORD", "")
_email = _load_yaml("email")
# Хост/порт IMAP/SMTP, алиас — БЕЗ дефолтов: заполняет визард установки/
# реконфигурации (setup_email_resume в awg-bot.sh). Пусто → фича спит.
EMAIL_IMAP_HOST: str = _email.get("imap_host", "")
EMAIL_IMAP_PORT: int = int(_email.get("imap_port", 993))
EMAIL_SMTP_HOST: str = _email.get("smtp_host", "")
EMAIL_SMTP_PORT: int = int(_email.get("smtp_port", 587))
# Адрес-алиас, на который клиент шлёт код (показывается в варнинге).
EMAIL_RESUME_ADDRESS: str = _email.get("resume_address", "")
# Интервал опроса IMAP, сек. Минимум 60 (чаще незачем): <60 → откат к 60.
_poll = int(_email.get("poll_interval_sec", 60))
EMAIL_POLL_INTERVAL_SEC: int = _poll if _poll >= 60 else 60
# Длина одноразового кода (символы из безопасного алфавита, без 0/O/1/l).
EMAIL_RESUME_CODE_LEN: int = int(_email.get("resume_code_len", 8))
# Email-выход активен, только если заданы креды И хосты (иначе фича спит).
EMAIL_RESUME_ENABLED: bool = bool(
    EMAIL_RESUME_LOGIN and EMAIL_RESUME_PASSWORD
    and EMAIL_IMAP_HOST and EMAIL_SMTP_HOST and EMAIL_RESUME_ADDRESS)


def validate() -> None:
    """Проверка обязательных параметров. Зовётся из main.py при старте (импорт
    модуля тестами/скриптами не требует заполненного конфига).

    Секреты — из окружения/.env; топология — из yaml (/etc/awg-bot/conf, генерит
    установщик). Пусто → падаем с указанием, ЧТО и ГДЕ задать."""
    missing_secrets = [n for n, v in (("BOT_TOKEN", BOT_TOKEN), ("ADMIN_ID", ADMIN_ID)) if not v]
    if missing_secrets:
        raise RuntimeError(
            "Не заданы обязательные секреты: " + ", ".join(missing_secrets) +
            ". Задайте их в /etc/awg-bot/env (или .env).")

    # Топология — обязательные ключи yaml. (param, значение, "файл: ключ")
    checks = [
        ("server_host", SERVER_HOST, "app.yaml: network.server_host"),
        ("server_port", SERVER_PORT, "app.yaml: network.server_port"),
    ]
    missing_conf = [where for _, val, where in checks if not val]
    if missing_conf:
        raise RuntimeError(
            "Не заданы обязательные параметры конфига: " + "; ".join(missing_conf) +
            ". Заполните их (обычно это делает установщик).")


# ─────────────────────────────────────────────────────────────────────────────
# app.yaml — инфраструктура
# ─────────────────────────────────────────────────────────────────────────────
_app = _load_yaml("app")

TZ_NAME = _app.get("timezone", "Europe/Moscow")
TZ = ZoneInfo(TZ_NAME)

_docker = _app.get("docker", {})
CONTAINER = _docker.get("container", "amnezia-awg2")
AWG_INTERFACE = _docker.get("interface", "awg0")
AWG_DIR = _docker.get("awg_dir", "/opt/amnezia/awg")
CONF_PATH = f"{AWG_DIR}/{AWG_INTERFACE}.conf"
CONF_BAK_PATH = f"{AWG_DIR}/{AWG_INTERFACE}.conf.bak"
CLIENTS_TABLE_PATH = f"{AWG_DIR}/clientsTable"
SERVER_PUBKEY_PATH = f"{AWG_DIR}/wireguard_server_public_key.key"
PSK_PATH = f"{AWG_DIR}/wireguard_psk.key"

_net = _app.get("network", {})
SUBNET_PREFIX = _net.get("subnet_prefix", "10.8.1")
# subnet_cidr из app.yaml Python-кодом не потребляется — информационное поле.
# install/harden_firewall.sh теперь выводит источник SSH-вайтлиста (bridge-подсеть
# контейнера) динамически из docker, а не из subnet_cidr (тот адрес не доезжает
# до хоста из-за MASQUERADE — см. reconcile_ssh_access).
IP_HOST_START = _net.get("ip_host_start", 1)
IP_HOST_END = _net.get("ip_host_end", 254)

# Порт SSH хоста. Общий источник истины для двух слоёв фильтра доступа к SSH:
# (1) хостовый вайтлист в harden_firewall.sh и (2) пер-пирный фильтр в контейнере
# (reconcile_ssh_access). Если развести — при нестандартном порте фильтр молча
# перестанет совпадать. Меняешь порт sshd — правь и здесь.
SSH_PORT = int(_net.get("ssh_port", 22))


# Деплой-топология этого хоста. Источник истины — yaml в /etc/awg-bot/conf:
# установщик генерит валидные значения под ответы админа, в эксплуатации админ
# правит их руками (на холодную; переживают обновление кода — код и conf в разных
# каталогах). env для топологии НЕ используется (env — только секреты). Обязательные
# ключи проверяет validate() при старте: пусто → падаем с внятным сообщением.
SERVER_HOST = _net.get("server_host", "")
SERVER_PORT = int(_net.get("server_port") or 0)

_cc = _app.get("client_config", {})
DNS1 = _cc.get("dns1", "1.1.1.1")
DNS2 = _cc.get("dns2", "1.0.0.1")
MTU = _cc.get("mtu", 1376)
KEEPALIVE_SECONDS = _cc.get("keepalive_seconds", 25)
CLIENT_ALLOWED_IPS = _cc.get("allowed_ips", "0.0.0.0/0, ::/0")
SERVER_NAME = _cc.get("server_name", "Сервер 1")

ONLINE_HANDSHAKE_SECONDS = _app.get("online_handshake_seconds", 300)

_sch = _app.get("scheduler", {})
TRAFFIC_POLL_MINUTES = _sch.get("traffic_poll_minutes", 5)
EXPIRY_CHECK_MINUTES = _sch.get("expiry_check_minutes", 60)
MONITOR_MINUTES = _sch.get("monitor_minutes", 3)
MONTHLY_RESET_DAY = _sch.get("monthly_reset_day", 1)
MONTHLY_RESET_HOUR = _sch.get("monthly_reset_hour", 0)
BACKUP_DAY = _sch.get("backup_day", 1)
BACKUP_HOUR = _sch.get("backup_hour", 12)
WATCHER_DEBOUNCE_SECONDS = _sch.get("watcher_debounce_seconds", 2)

_mis = _app.get("misfire_grace", {})
MISFIRE_GRACE_INTERVAL_SECONDS = _mis.get("interval_seconds", 120)
MISFIRE_GRACE_EXPIRY_SECONDS = _mis.get("expiry_seconds", 300)
MISFIRE_GRACE_CRON_SECONDS = _mis.get("cron_seconds", 3600)

MISSING_SWEEPS_THRESHOLD = _app.get("missing_sweeps_threshold", 2)

_hist = _app.get("history", {})
HISTORY_RETENTION_YEARS = _hist.get("retention_years", 2)
HISTORY_PURGE_HOUR = _hist.get("purge_hour", 3)
HISTORY_PURGE_BATCH_SIZE = _hist.get("purge_batch_size", 500)


# ─────────────────────────────────────────────────────────────────────────────
# subscription.yaml
# ─────────────────────────────────────────────────────────────────────────────
_sub = _load_yaml("subscription")
PERIOD_CHOICES = _sub.get("period_choices", ["day", "week", "month", "year", "never"])
PERIOD_LABELS = _sub.get("period_labels", {})
NOTIFY_THRESHOLDS_MINUTES = [tuple(x) for x in _sub.get("notify_thresholds_minutes", [])]


# ─────────────────────────────────────────────────────────────────────────────
# limits.yaml
# ─────────────────────────────────────────────────────────────────────────────
_lim = _load_yaml("limits")
TRAFFIC_BONUS_GB = _lim.get("traffic_bonus_gb", 100)
TRAFFIC_WARN_PERCENT = _lim.get("traffic_warn_percent", 80)


# ─────────────────────────────────────────────────────────────────────────────
# grace.yaml / pause.yaml
# ─────────────────────────────────────────────────────────────────────────────
GRACE_DAYS = _load_yaml("grace").get("grace_days", 14)

_pause = _load_yaml("pause")
PAUSE_MAX_TOTAL_DAYS = _pause.get("pause_max_total_days", 28)


# ─────────────────────────────────────────────────────────────────────────────
# quiet_hours.yaml
# ─────────────────────────────────────────────────────────────────────────────
_qh = _load_yaml("quiet_hours")
QUIET_HOURS_ENABLED = _qh.get("quiet_hours_enabled", True)
QUIET_HOURS_START = _qh.get("quiet_hours_start", 20)
QUIET_HOURS_END = _qh.get("quiet_hours_end", 7)


# ─────────────────────────────────────────────────────────────────────────────
# Мониторинг co-located хоста (метрики читаем локально: /proc + statvfs)
# ─────────────────────────────────────────────────────────────────────────────
_mon = _app.get("monitoring") or {}
SERVICE_FAILURE_ALERT_LOUD = _mon.get("service_failure_alert_loud", True)
SERVICE_FAILURE_ALERT_MINUTES = _mon.get("service_failure_alert_minutes", 5)
# Стрик гистерезиса ресурс-алертов: столько замеров подряд (тиков монитора)
# нужно для срабатывания «высокая загрузка» и столько же — для отбоя.
RESOURCE_ALERT_STREAK = _mon.get("alert_streak", 5)


# ─────────────────────────────────────────────────────────────────────────────
# resource_alerts.yaml — алерты о загрузке хоста (CPU/RAM/диск)
# ─────────────────────────────────────────────────────────────────────────────
_ra = _load_yaml("resource_alerts")
RESOURCE_ALERTS_ENABLED = _ra.get("enabled", True)
_ra_th = _ra.get("thresholds_percent", {})
RESOURCE_ALERT_CPU_PERCENT = _ra_th.get("cpu", 80)
RESOURCE_ALERT_RAM_PERCENT = _ra_th.get("ram", 80)
RESOURCE_ALERT_DISK_PERCENT = _ra_th.get("disk", 80)


# ─────────────────────────────────────────────────────────────────────────────
# bot_identity.yaml — публичное имя и описания бота (маскирующие формулировки)
# ─────────────────────────────────────────────────────────────────────────────
_bi = _load_yaml("bot_identity")
BOT_NAME = _bi.get("name", "")
BOT_DESCRIPTION = _bi.get("description", "")
BOT_SHORT_DESCRIPTION = _bi.get("short_description", "")


__all__ = [name for name in dir() if not name.startswith("_")]
