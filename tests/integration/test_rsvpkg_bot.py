"""Часть бота пакета переезда (util/rsvpkg/bot.py) на настоящей базе:
реплика без истории и лишних секретов, настройки по белому списку, env.

Сторож «чего нет никогда»: база заполняется тем, что живёт на работающем
сервере, — истории, закрытые ключи устройств, фраза и ключ бэкапов, заявки
и ключи сопряжения шлюзов, состояние обновлений, набора и самого резерва,
токены ботов шлюзов, — и всё это обязано не доехать до резерва. Белый
список `server_state` проверяется в обратную сторону: ключ, о котором
пакет ничего не знает (появится в следующих версиях), в реплику не попадает.

Цена ошибки: фраза бэкапов в реплике — резерв (другой человек) читает все
бэкапы основы; закрытые ключи устройств — резерв выдаёт себя за клиентов
основы; токен бота шлюза — резерв управляет шлюзом основы; история — месяцы
персональных данных у постороннего сервера без нужды.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.infra.db.schema.selects import HISTORY_TABLES
from awgbot.util.rsvpkg import bot

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]

SECRET_STATE = {
    "backup_passphrase": "DUMMY-phrase",
    "backup_key": "DUMMY-backup-key",
    "backup_event_due": "1",
    "gw_join": '{"id": "DUMMY"}',
    "gw_join_1": '{"priv": "DUMMY"}',
    "gw_keep_base": '{"slot": 1}',
    "gwcfg_ack_1": "{}",
    "gwlink_rekey_1": "{}",
    "update_meta": '{"version": "4.0.0"}',
    "updates_seen": "4.0.0",
    "fleet_target": "4.0.0",
    "rsv_pkg_due": "1",
    "rsv_keys": '{"m": "DUMMY"}',
    "restore_pending": "1",
    "GW_BOT_TOKEN": "123456789:AA-DUMMY",
    "GW_BOT_TOKEN_2": "123456789:BB-DUMMY",
    "host_boot_id": "DUMMY",
    "admin_bootstrap_device": "1",
    "reserve_new_feature_4_1": "что-то из будущей версии",
}
GUEST_STATE = {"email_login": "box@example.org", "email_password": "DUMMY-pass",
               "routing_active_gateway": "1", "private_dns_decision": "keep"}


def _fill_history(con) -> None:
    """По строке в каждую таблицу истории — значениями под типы колонок."""
    for t in HISTORY_TABLES:
        cols = [r for r in con.execute(f"PRAGMA table_info({t})") if not r[5]]   # без PRIMARY KEY
        vals = []
        for _cid, _name, typ, _nn, _dflt, _pk in cols:
            vals.append(1 if "INT" in (typ or "").upper() else "2026-10")
        con.execute(f"INSERT INTO {t} ({', '.join(c[1] for c in cols)}) VALUES ({', '.join('?' * len(cols))})",
                    vals)


@pytest.fixture()
def live(services, db, make_active_client, fake_awg):
    """База работающего сервера: клиент с устройствами (закрытые ключи),
    слот шлюза, меню в чате, истории, секреты в server_state."""
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    services.add_device(admin.id, "iPhone")
    client = make_active_client(name="Профиль", tg_id=555001, device_limit=3)
    services.add_device(client.id, "Телефон")
    db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    db.set_nav_message_id(555001, 4242)
    for k, v in {**SECRET_STATE, **GUEST_STATE}.items():
        db.set_state(k, v)
    with sqlite3.connect(db.path) as con:
        _fill_history(con)
    return db


def _db_path(db) -> str:
    return db.path


def _open(raw: bytes, tmp_path) -> sqlite3.Connection:
    p = tmp_path / "replica.db"
    p.write_bytes(raw)
    return sqlite3.connect(p)


def test_the_live_base_really_has_what_must_not_travel(live):
    """Предпосылка сторожа: в исходной базе всё запретное на месте —
    иначе следующий тест зелёный впустую."""
    con = sqlite3.connect(_db_path(live))
    assert con.execute("SELECT COUNT(*) FROM devices WHERE private_key IS NOT NULL AND private_key != ''").fetchone()[0] >= 3
    for t in HISTORY_TABLES:
        assert con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] >= 1, t
    keys = {r[0] for r in con.execute("SELECT key FROM server_state")}
    assert set(SECRET_STATE) <= keys


def test_the_replica_has_no_history_no_device_keys_and_only_whitelisted_state(live, tmp_path):
    raw = bot.replica(_db_path(live))
    bot.assert_clean(raw)
    con = _open(raw, tmp_path)
    for t in HISTORY_TABLES:
        assert con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, f"история {t} доехала до резерва"
    keys = [r[0] for r in con.execute("SELECT private_key FROM devices")]
    assert keys and all(k is None for k in keys), "закрытые ключи устройств доехали до резерва"
    state = dict(con.execute("SELECT key, value FROM server_state"))
    leaked = sorted(set(state) & set(SECRET_STATE))
    assert not leaked, f"в реплике: {leaked}"
    assert set(state) <= set(bot.GUEST_STATE), f"ключи вне белого списка: {sorted(set(state) - set(bot.GUEST_STATE))}"
    assert state.get("email_login") == "box@example.org", "почта для выхода из паузы не доехала"


def test_what_the_base_bot_needs_travels(live, tmp_path):
    """Профили, устройства с открытыми ключами и адресами, слоты шлюзов и
    меню клиентов — без них бот основы на резерве не обслужит никого."""
    src = sqlite3.connect(_db_path(live))
    con = _open(bot.replica(_db_path(live)), tmp_path)
    for table, cols in (("clients", "id, tg_id, name"), ("devices", "id, public_key, address, preshared_key"),
                        ("gateways", "id, link_if, link_port, link_cidr"), ("ui_state", "chat_id, nav_message_id")):
        want = src.execute(f"SELECT {cols} FROM {table} ORDER BY 1").fetchall()
        assert want, f"в исходной базе пусто: {table}"
        assert con.execute(f"SELECT {cols} FROM {table} ORDER BY 1").fetchall() == want, f"{table} не доехала"


def test_the_replica_does_not_touch_the_live_base(live):
    before = sqlite3.connect(_db_path(live)).execute("SELECT COUNT(*) FROM server_state").fetchone()[0]
    bot.replica(_db_path(live))
    con = sqlite3.connect(_db_path(live))
    assert con.execute("SELECT COUNT(*) FROM server_state").fetchone()[0] == before
    assert con.execute("SELECT COUNT(*) FROM devices WHERE private_key IS NOT NULL").fetchone()[0] >= 3


@pytest.mark.parametrize("dirt", [
    "INSERT INTO server_state (key, value) VALUES ('backup_passphrase', 'DUMMY')",
    "INSERT INTO server_state (key, value) VALUES ('something_new', '1')",
    "UPDATE devices SET private_key = 'DUMMY'",
    "history",
])
def test_the_guard_refuses_a_dirty_replica(live, tmp_path, dirt):
    """Сторож — последний рубеж: если очистка когда-нибудь пропустит лишнее,
    сборка пакета падает, а не уходит резерву."""
    con = _open(bot.replica(_db_path(live)), tmp_path)
    if dirt == "history":
        _fill_history(con)
    else:
        con.execute(dirt)
    con.commit()
    con.close()
    with pytest.raises(bot.BotPartError):
        bot.assert_clean((tmp_path / "replica.db").read_bytes())


def test_the_whole_bot_part_carries_two_env_lines_only(live, monkeypatch):
    part = bot.build(_db_path(live), str(ROOT / "conf"), token="123456789:AA-DUMMY", admin_id=config.ADMIN_ID)
    assert part["env"] == {"BOT_TOKEN": "123456789:AA-DUMMY", "ADMIN_ID": config.ADMIN_ID}
    assert bytes.fromhex(part["db"])[:16] == b"SQLite format 3\x00"


# ── настройки по белому списку ───────────────────────────────────────────────

def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def test_the_repository_conf_travels_without_paths_and_without_host_sections():
    conf = bot.conf_part(str(ROOT / "conf"))
    assert set(conf) <= set(bot.CONF_FILES) | {"app"}, f"лишние файлы: {sorted(set(conf) - set(bot.CONF_FILES) - {'app'})}"
    assert "bot_identity" not in conf and "updates" not in conf
    for section in ("awg", "docker", "network", "reserve", "bot_identity", "gateway", "backup", "updates"):
        assert section not in conf["app"], f"раздел app.{section} доехал до резерва"
    assert {"client_config", "routing"} <= set(conf["app"]), "тарифы и routing.* — то, ради чего часть есть"
    paths = [(p, v) for p, v in _walk(conf) if isinstance(v, str) and v.startswith("/")]
    assert not paths, f"значения-пути доехали: {paths[:5]}"


def test_paths_and_file_keys_are_dropped_wherever_they_sit(tmp_path):
    (tmp_path / "app.yaml").write_text(
        "routing:\n"
        "  enabled: true\n"
        "  dnsmasq_conf: /etc/dnsmasq.d/awg.conf\n"
        "  lists_dir: /var/lib/awg-bot/lists\n"
        "  nested:\n"
        "    conf_dir: relative/but/forbidden\n"
        "    keep: 5\n"
        "  hosts: [/etc/hosts, 198.51.100.7]\n"
        "client_config:\n"
        "  keepalive_seconds: \"25-35\"\n"
        "network:\n"
        "  server_host: vpn.example.net\n",
        encoding="utf-8")
    (tmp_path / "limits.yaml").write_text("max_devices: 5\nlog: /var/log/x\n", encoding="utf-8")
    (tmp_path / "bot_identity.yaml").write_text("name: секрет\n", encoding="utf-8")
    conf = bot.conf_part(str(tmp_path))
    assert conf["app"]["routing"] == {"enabled": True, "nested": {"keep": 5}, "hosts": ["198.51.100.7"]}
    assert conf["app"]["client_config"] == {"keepalive_seconds": "25-35"}
    assert "network" not in conf["app"]
    assert conf["limits"] == {"max_devices": 5}
    assert "bot_identity" not in conf


def test_a_broken_or_missing_conf_file_is_skipped_not_fatal(tmp_path):
    (tmp_path / "limits.yaml").write_text("max_devices: [1,\n", encoding="utf-8")
    (tmp_path / "pause.yaml").write_text("- просто список\n", encoding="utf-8")
    conf = bot.conf_part(str(tmp_path))
    assert "limits" not in conf and "pause" not in conf and conf.get("app") == {}
