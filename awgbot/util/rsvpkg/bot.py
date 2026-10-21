"""
bot.py — часть бота пакета: реплика БД, значения настроек по белому списку, env.

Реплика — согласованный снимок базы (backup API) без таблиц истории и без
того, что резерву не нужно никогда: закрытых ключей устройств, фразы и ключа
бэкапов, ключей слотов и заявок сопряжения, состояния обновлений и самого
резерва, токенов ботов шлюзов. Из `server_state` едут только ключи белого
списка `GUEST_STATE`; новый ключ, не названный здесь, в пакет не попадает —
сторож в тестах это проверяет по реальной базе.

Настройки — значения из `conf/*.yaml` по белому списку файлов; строки,
похожие на пути, отбрасываются: файловые ключи резерв ставит сам.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from awgbot.infra.db.schema.selects import HISTORY_TABLES

# ключи server_state, которые едут в реплику (остальные — нет)
GUEST_STATE: tuple[str, ...] = (
    "email_check", "email_login", "email_password",       # ящик и код выхода из паузы
    "private_dns_decision",                               # решение админа о резолвере
    "routing_active_gateway", "routing_manual_hold",      # активный слот и ручной выбор
    "move_notified",                                      # «переподключи VPN» уже ушло (один раз на переезд)
)
# что не едет никогда — сторож: ключ с таким началом в реплике — ошибка сборки
FORBIDDEN_STATE: tuple[str, ...] = ("backup_", "gw_", "gwcfg_", "gwlink_", "gwupd", "update_", "updates_",
                                    "fleet_", "rsv_", "restore_", "host_boot_id", "admin_bootstrap_device")
# файлы настроек, значения которых едут (`conf/<имя>.yaml`)
CONF_FILES: tuple[str, ...] = ("subscription", "grace", "limits", "pause", "quiet_hours", "notifications",
                               "email", "resource_alerts")
# разделы app.yaml, которые едут (без путей)
APP_SECTIONS: tuple[str, ...] = ("client_config", "routing", "bonus", "timezone")
FORBIDDEN_CONF: tuple[str, ...] = ("awg", "network", "docker", "backup", "gateway", "updates", "reserve",
                                   "bot_identity", "dnsmasq_conf", "dnsmasq_service", "conf_dir")
ENV_KEYS: tuple[str, ...] = ("BOT_TOKEN", "ADMIN_ID")
# Ключи app.yaml из запрещённых разделов, которые гостю всё же нужны: без них
# бот основы на резерве не соберёт ссылки и конфигурации клиентов (адрес,
# порт и подсеть — те же, что у основы: клон несёт её интерфейс) и не отличит
# своего протокола. Ровно этот перечень, ничего с путями.
APP_GUEST_KEYS: dict[str, tuple[str, ...]] = {
    "awg": ("protocol_id",),
    "network": ("server_host", "server_port", "subnet_prefix", "subnet_cidr", "ip_host_start", "ip_host_end"),
}


class BotPartError(ValueError):
    """Часть бота не собралась или не прошла сторожа."""


def _looks_like_path(v) -> bool:
    return isinstance(v, str) and v.startswith("/")


def _clean(obj):
    """Снять пути и запрещённые ключи рекурсивно."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()
                if k not in FORBIDDEN_CONF and not _looks_like_path(v)}
    if isinstance(obj, list):
        return [_clean(v) for v in obj if not _looks_like_path(v)]
    return obj


def conf_part(conf_dir: str) -> dict:
    """Значения настроек по белому списку: {имя файла: {…}}, app — только разделы."""
    import yaml
    out: dict = {}
    for name in CONF_FILES:
        path = os.path.join(conf_dir, f"{name}.yaml")
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict):
            out[name] = _clean(data)
    try:
        with open(os.path.join(conf_dir, "app.yaml"), encoding="utf-8") as f:
            app = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        app = {}
    if isinstance(app, dict):
        out["app"] = {k: _clean(app[k]) for k in APP_SECTIONS if k in app}
    return out


def guest_part(conf_dir: str) -> dict:
    """Ключи запрещённых разделов app.yaml, нужные гостю (APP_GUEST_KEYS):
    только скаляры, не пути. Отдельная часть бота (`app_guest`), не внутри
    значений настроек: сторож «разделов хоста в настройках нет» остаётся строгим."""
    import yaml
    try:
        with open(os.path.join(conf_dir, "app.yaml"), encoding="utf-8") as f:
            app = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out: dict = {}
    for sec, keys in APP_GUEST_KEYS.items():
        src = app.get(sec) if isinstance(app, dict) else None
        if not isinstance(src, dict):
            continue
        vals = {k: src[k] for k in keys if k in src and isinstance(src[k], (str, int, float, bool))
                and not _looks_like_path(src[k])}
        if vals:
            out[sec] = vals
    return out


LISTS_MAX = 200_000                  # строк в кэше одного вида — потолок части


def lists_part(data_dir: str) -> dict:
    """Кэш списков РФ-доступа (`routing-<вид>.lst` в каталоге данных): {вид:
    [строки]} — гостю, у которого источники списков недоступны (HOST_ONLY)."""
    out: dict = {}
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return out
    for name in names:
        if not name.startswith("routing-") or not name.endswith(".lst"):
            continue
        kind = name[len("routing-"):-len(".lst")]
        if not kind.replace("_", "").isalnum():
            continue
        try:
            with open(os.path.join(data_dir, name), encoding="utf-8") as f:
                lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            continue
        out[kind] = lines[:LISTS_MAX]
    return out


def env_part(token: str, admin_id: int) -> dict:
    return {"BOT_TOKEN": str(token or ""), "ADMIN_ID": int(admin_id or 0)}


def replica(db_path: str) -> bytes:
    """Снимок базы без истории и лишних секретов — байты файла SQLite."""
    fd, tmp = tempfile.mkstemp(prefix="rsv-rep-", suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode = DELETE")
                _strip(dst)
                dst.commit()
                dst.execute("VACUUM")
            finally:
                dst.close()
        finally:
            src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(tmp + suffix)
            except OSError:
                pass


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _strip(con) -> None:
    have = _tables(con)
    for t in HISTORY_TABLES:
        if t in have:
            con.execute(f"DELETE FROM {t}")
    if "devices" in have and any(r[1] == "private_key" for r in con.execute("PRAGMA table_info(devices)")):
        con.execute("UPDATE devices SET private_key = NULL")
    # локальные сети и подписи шлюзов основы, строка пары (с админом резерва) — не резерву
    for sql in ("UPDATE gateways SET lan_subnets = '[]', label = '', lan_mode = 0",
                "DELETE FROM reserve_peer"):
        try:
            con.execute(sql)
        except sqlite3.OperationalError:
            pass
    if "server_state" in have:
        marks = ",".join("?" * len(GUEST_STATE))
        con.execute(f"DELETE FROM server_state WHERE key NOT IN ({marks})", GUEST_STATE)
    if "ui_state" in have:
        pass                                             # меню клиентов — едет (§2.4)


def assert_clean(db_bytes: bytes) -> None:
    """Сторож реплики: истории пусты, закрытых ключей нет, в state — только белый
    список. Не так — BotPartError (сборка не должна пройти молча)."""
    fd, tmp = tempfile.mkstemp(prefix="rsv-chk-", suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(db_bytes)
        con = sqlite3.connect(tmp)
        try:
            have = _tables(con)
            for t in HISTORY_TABLES:
                if t in have and con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]:
                    raise BotPartError(f"в реплике осталась история: {t}")
            if "devices" in have and con.execute(
                    "SELECT COUNT(*) FROM devices WHERE private_key IS NOT NULL AND private_key != ''").fetchone()[0]:
                raise BotPartError("в реплике остались закрытые ключи устройств")
            if "server_state" in have:
                for (key,) in con.execute("SELECT key FROM server_state"):
                    if key not in GUEST_STATE or key.startswith(FORBIDDEN_STATE):
                        raise BotPartError(f"в реплике лишний ключ состояния: {key}")
        finally:
            con.close()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def build(db_path: str, conf_dir: str, *, token: str, admin_id: int) -> dict:
    """Часть бота целиком: реплика (hex), настройки, env."""
    db = replica(db_path)
    assert_clean(db)
    return {"db": db.hex(), "conf": conf_part(conf_dir), "env": env_part(token, admin_id)}
