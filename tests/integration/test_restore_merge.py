"""
Слияние бэкапа с текущим (domain/backup/merge, tools/restore_merge): настоящие
базы SQLite и файлы во временном каталоге.

Инварианты:
- связность, та же линия: у каждого слота, оставленного текущим, конфиги линка
  обеих сторон, строка слота, устройство и его [Peer], [Interface] клиентского
  конфига, env, сетевые параметры сервера и слотовые ключи CURRENT — байт в байт
  текущие;
- связность, другая линия (новый сервер): [Interface], пиры, подсеть, порт,
  линки, слоты и токены ботов шлюзов — из бэкапа; адрес сервера, токен бота и
  ADMIN_ID — текущие;
- время: расход, сроки, блокировки, бонусы, отметки, погашенные коды, ключи
  устройств — текущие; настройки — из бэкапа; истёкшее к моменту
  восстановления на другой линии — истёкшее и без повторных уведомлений.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

ADMIN = config.ADMIN_ID
APP = ("network:\n  subnet_prefix: \"{prefix}\"\n  subnet_cidr: \"{prefix}.0/24\"\n  server_host: \"{host}\"\n"
       "  server_port: {port}\nrouting:\n  link_channel_port: {chport}\nscheduler:\n  backup_day: {day}\n")


def _sql(db, q, *a):
    con = db._connection()
    con.execute(q, a)
    con.commit()


def _one(path, q, *a):
    con = sqlite3.connect(path)
    try:
        row = con.execute(q, a).fetchone()
        return row if row is None or len(row) > 1 else row[0]
    finally:
        con.close()


class World:
    """Сервер AWG во временном каталоге: база сервисов, клиентский конфиг,
    линки слотов, выданные шлюзовые стороны, app.yaml, env."""

    def __init__(self, services, root: Path, *, prefix="10.8.1", host="vpn.example.org", port=51820,
                 chport=8787, server_key="DUMMY-SERVER-KEY", bot_token="DUMMY-BOT-1"):
        self.s, self.db = services, services.db
        self.root = root
        self.awg = root / "awg"; self.awg.mkdir(parents=True)
        self.gwroot = root / "root"; self.gwroot.mkdir()
        self.conf = root / "conf"; self.conf.mkdir()
        self.env = root / "env"
        self.server_key = server_key
        (self.conf / "app.yaml").write_text(APP.format(prefix=prefix, host=host, port=port, chport=chport, day=1),
                                            encoding="utf-8")
        (self.conf / "email.yaml").write_text("smtp_port: 587\n", encoding="utf-8")
        self.env.write_text(f"BOT_TOKEN={bot_token}\nADMIN_ID={ADMIN}\n", encoding="utf-8")

    def env_set(self, key, val):
        lines = [x for x in self.env.read_text(encoding="utf-8").splitlines() if not x.startswith(key + "=")]
        self.env.write_text("\n".join(lines + [f"{key}={val}"]) + "\n", encoding="utf-8")

    def link(self, slot: int, gw_pub: str, vps_pub: str):
        name = "awglink" if slot == 1 else f"awglink{slot}"
        (self.awg / f"{name}.conf").write_text(
            f"[Interface]\nPrivateKey = DUMMY-VPS-{vps_pub}\nListenPort = {442 + slot}\n\n[Peer]\nPublicKey = {gw_pub}\n",
            encoding="utf-8")
        (self.gwroot / f"gw-{name}.conf").write_text(
            f"[Interface]\nPrivateKey = DUMMY-GW-{gw_pub}\n\n[Peer]\nPublicKey = {vps_pub}\n", encoding="utf-8")
        return name

    def slot(self, slot: int, device_id: int, gw_pub: str = "", vps_pub: str = ""):
        name = self.link(slot, gw_pub or f"GWPUB{slot}", vps_pub or f"VPSPUB{slot}")
        self.db.gateway_add(device_id, name, 442 + slot, f"10.99.99.{(slot - 1) * 4}/30", slot_id=slot)
        self.env_set("GW_BOT_TOKEN" if slot == 1 else f"GW_BOT_TOKEN_{slot}", f"DUMMY-GWBOT-{slot}")
        return name

    def write_client_conf(self):
        """Клиентский конфиг — по составу устройств базы, как его держит бот."""
        lines = [f"[Interface]\nPrivateKey = {self.server_key}\nListenPort = 51820\nAddress = 10.8.1.1/24\n"]
        for d in self.db.list_all_devices():
            lines.append(f"[Peer]\nPublicKey = {d.public_key}\nPresharedKey = {d.preshared_key}\n"
                         f"AllowedIPs = {d.address}/32\n")
        (self.awg / "awg0.conf").write_text("\n".join(lines), encoding="utf-8")

    def take_copy(self, dest: Path) -> Path:
        """Бэкап этого сервера — распакованным, как его видит слияние."""
        from awgbot.domain.backup import meta, merge
        (dest / "state" / "conf").mkdir(parents=True)
        merge.data.snapshot(self.db.path, str(dest / "state" / "bot.db"))
        for f in self.conf.iterdir():
            shutil.copyfile(f, dest / "state" / "conf" / f.name)
        shutil.copyfile(self.env, dest / "state" / "env")
        shutil.copytree(self.awg, dest / "awg")
        shutil.copytree(self.gwroot, dest / "root")
        m = meta.build(self.db, "main")
        m["gateways"] = meta.gateways(self.db, awg_dir=str(self.awg), root_dir=str(self.gwroot))
        (dest / meta.META_NAME).write_text(json.dumps(m), encoding="utf-8")
        return dest

    def run(self, copy: Path) -> dict:
        from awgbot.domain.backup import merge
        return merge.run(str(copy), cur_db=self.db.path, conf_dir=str(self.conf), env_path=str(self.env),
                         awg_dir=str(self.awg), root_dir=str(self.gwroot), client_if="awg0")

    def files(self) -> dict[str, bytes]:
        out = {}
        for base, pref in ((self.awg, "awg/"), (self.gwroot, "root/")):
            for f in sorted(base.iterdir()):
                out[pref + f.name] = f.read_bytes()
        return out


def _copy_files(copy: Path) -> dict[str, bytes]:
    out = {}
    for sub in ("awg", "root"):
        d = copy / sub
        if d.is_dir():
            for f in sorted(d.iterdir()):
                out[f"{sub}/{f.name}"] = f.read_bytes()
    return out


@pytest.fixture()
def world(services, make_active_client, monkeypatch, tmp_path, fake_awg):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "3.4.0")
    w = World(services, tmp_path / "live")
    monkeypatch.setattr(config, "AWG_DIR", str(w.awg))
    w.admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    w.gw1 = services.add_device(w.admin.id, "NASPi").device_id
    w.slot(1, w.gw1)
    w.ivan = make_active_client(name="Иван", tg_id=2001)
    w.phone = services.add_device(w.ivan.id, "Телефон").device_id
    w.laptop = services.add_device(w.ivan.id, "Ноутбук").device_id
    w.write_client_conf()
    return w


# ── та же линия: связность текущая ───────────────────────────────────────────

def test_same_line_keeps_the_connectivity_of_live_slots_byte_for_byte(world, tmp_path):
    copy = world.take_copy(tmp_path / "copy")
    # после бэкапа: ключи линка сменились, ключи телефона перевыпущены, порт канала сменён
    world.link(1, "GWPUB1-NEW", "VPSPUB1-NEW")
    _sql(world.db, "UPDATE devices SET public_key = 'PHONE-NEW-PUB', preshared_key = 'PHONE-NEW-PSK' WHERE id = ?",
         world.phone)
    world.write_client_conf()
    (world.conf / "app.yaml").write_text(APP.format(prefix="10.8.1", host="vpn.example.org", port=51820,
                                                    chport=9797, day=5), encoding="utf-8")
    world.db.set_state("gwlink_seen_1", "2026-10-17T10:00:00+03:00")
    world.db.set_state("gwcfg_hash_1", "cur-hash")
    live_files, live_env = world.files(), world.env.read_bytes()
    gw_row = _one(world.db.path, "SELECT * FROM gateways WHERE id = 1")
    report = world.run(copy)
    assert report["line"] == "same"
    assert _copy_files(copy) == live_files, "файлы интерфейсов и линков не равны текущим"
    assert (copy / "state" / "env").read_bytes() == live_env
    res = str(copy / "state" / "bot.db")
    assert _one(res, "SELECT * FROM gateways WHERE id = 1") == gw_row
    assert _one(res, "SELECT public_key, preshared_key, address FROM devices WHERE id = ?", world.phone) == \
        ("PHONE-NEW-PUB", "PHONE-NEW-PSK", world.db.get_device(world.phone).address)
    assert _one(res, "SELECT value FROM server_state WHERE key = 'gwlink_seen_1'") == "2026-10-17T10:00:00+03:00"
    assert _one(res, "SELECT value FROM server_state WHERE key = 'gwcfg_hash_1'") == "cur-hash"
    import yaml
    app = yaml.safe_load((copy / "state" / "conf" / "app.yaml").read_text(encoding="utf-8"))
    assert app["routing"]["link_channel_port"] == 9797, "порт канала — текущий на той же линии"
    assert app["scheduler"]["backup_day"] == 1, "настройка вне связности — из бэкапа"
    assert app["network"]["server_host"] == "vpn.example.org" and app["network"]["subnet_cidr"] == "10.8.1.0/24"
    assert [s["case"] for s in report["slots"]] == ["rekeyed"]


def test_same_line_time_stays_current_and_settings_come_from_the_copy(world, tmp_path):
    db = world.db
    # в бэкапе — настройки на тот момент
    _sql(db, "UPDATE clients SET name = 'Иван (бэкап)', device_limit = 5, routing_allowed = 1 WHERE id = ?", world.ivan.id)
    _sql(db, "UPDATE client_quota SET traffic_limit = 111 WHERE client_id = ?", world.ivan.id)
    _sql(db, "UPDATE devices SET name = 'Телефон (бэкап)', routing_on = 1 WHERE id = ?", world.phone)
    _sql(db, "INSERT INTO client_routing_domains VALUES (?, 'ozon.ru', '2026-10-01T00:00:00+03:00')", world.ivan.id)
    db.set_state("private_dns_decision", "copy-decision")
    db.set_state("email_login", "box@example.org")
    copy = world.take_copy(tmp_path / "copy")
    # после бэкапа — случившееся и правки настроек
    _sql(db, "UPDATE clients SET name = 'Иван (сейчас)', device_limit = 2, routing_allowed = 0, "
             "block_reason = ? WHERE id = ?", int(ClientBlock.TRAFFIC_CLIENT), world.ivan.id)
    _sql(db, "UPDATE client_subscription SET period_end = '2027-10-01T00:00:00+03:00', notified_thresholds = '43200' "
             "WHERE client_id = ?", world.ivan.id)
    _sql(db, "UPDATE client_quota SET traffic_limit = 222, bonus_bytes = 5, bonus_granted_month = 1 WHERE client_id = ?",
         world.ivan.id)
    _sql(db, "UPDATE devices SET name = 'Телефон (сейчас)', routing_on = 0, block_reason = ? WHERE id = ?",
         int(DeviceBlock.TRAFFIC_CLIENT), world.phone)
    _sql(db, "UPDATE device_traffic SET traffic_rx_month = 999 WHERE device_id = ?", world.phone)
    _sql(db, "DELETE FROM client_routing_domains")
    _sql(db, "INSERT INTO traffic_samples VALUES (?, 7, 7, '2026-10-17T00:00:00+03:00')", world.phone)
    for key, val in (("last_monthly_reset", "2026-10"), ("gw_claim_nonces", "n1 n2"), ("update_pending", "v3.4.1"),
                     ("backup_passphrase", "current-phrase"), ("res_alert_cpu", "1"), ("private_dns_decision", "cur"),
                     ("email_login", "")):
        db.set_state(key, val)
    world.run(copy)
    res = str(copy / "state" / "bot.db")
    assert _one(res, "SELECT name, device_limit, routing_allowed, block_reason FROM clients WHERE id = ?",
                world.ivan.id) == ("Иван (бэкап)", 5, 1, int(ClientBlock.TRAFFIC_CLIENT))
    assert _one(res, "SELECT period_end, notified_thresholds FROM client_subscription WHERE client_id = ?",
                world.ivan.id) == ("2027-10-01T00:00:00+03:00", "43200"), "продление после бэкапа потеряно"
    assert _one(res, "SELECT traffic_limit, bonus_bytes, bonus_granted_month FROM client_quota WHERE client_id = ?",
                world.ivan.id) == (111, 5, 1)
    assert _one(res, "SELECT name, routing_on, block_reason FROM devices WHERE id = ?", world.phone) == \
        ("Телефон (бэкап)", 1, int(DeviceBlock.TRAFFIC_CLIENT))
    assert _one(res, "SELECT traffic_rx_month FROM device_traffic WHERE device_id = ?", world.phone) == 999
    assert _one(res, "SELECT domain FROM client_routing_domains WHERE client_id = ?", world.ivan.id) == "ozon.ru"
    assert _one(res, "SELECT last_rx FROM traffic_samples WHERE device_id = ?", world.phone) == 7
    state = dict(sqlite3.connect(res).execute("SELECT key, value FROM server_state").fetchall())
    assert (state["last_monthly_reset"], state["gw_claim_nonces"], state["update_pending"],
            state["backup_passphrase"], state["res_alert_cpu"]) == ("2026-10", "n1 n2", "v3.4.1", "current-phrase", "1")
    assert state["private_dns_decision"] == "copy-decision" and state["email_login"] == "box@example.org"


def test_same_line_composition_stays_current(world, services, make_active_client, tmp_path):
    copy = world.take_copy(tmp_path / "copy")
    piotr = make_active_client(name="Пётр", tg_id=2002)
    tab = services.add_device(piotr.id, "Планшет").device_id
    services.db.delete_device(world.laptop)
    world.write_client_conf()
    world.run(copy)
    res = str(copy / "state" / "bot.db")
    assert _one(res, "SELECT name FROM clients WHERE id = ?", piotr.id) == "Пётр"
    assert _one(res, "SELECT public_key FROM devices WHERE id = ?", tab) == services.db.get_device(tab).public_key
    assert _one(res, "SELECT id FROM devices WHERE id = ?", world.laptop) is None, "удалённое после бэкапа вернулось"
    conf = (copy / "awg" / "awg0.conf").read_text(encoding="utf-8")
    assert services.db.get_device(tab).public_key in conf


def test_an_invite_accepted_after_the_copy_stays_accepted(world, services, tmp_path):
    created = services.create_client("Ольга", 1, "year", 0)
    copy = world.take_copy(tmp_path / "copy")
    assert services.activate_client(created.invite_code, 3003).ok
    world.run(copy)
    res = str(copy / "state" / "bot.db")
    assert _one(res, "SELECT tg_id, activation_status, invite_code FROM clients WHERE id = ?", created.client_id) == \
        (3003, "active", None), "погашенный код ожил"


def test_a_slot_removed_after_the_copy_does_not_come_back(world, services, tmp_path):
    gw2 = services.add_device(world.admin.id, "Pi4").device_id
    world.slot(2, gw2)
    copy = world.take_copy(tmp_path / "copy")
    world.s._run_link_script = lambda mode, env=None: None
    world.s.gateway_remove(2)
    for f in (world.awg / "awglink2.conf", world.gwroot / "gw-awglink2.conf"):
        f.unlink()
    world.env_set("GW_BOT_TOKEN_2", "")
    world.env.write_text("\n".join(x for x in world.env.read_text(encoding="utf-8").splitlines()
                                   if not x.startswith("GW_BOT_TOKEN_2")) + "\n", encoding="utf-8")
    report = world.run(copy)
    res = str(copy / "state" / "bot.db")
    assert _one(res, "SELECT id FROM gateways WHERE id = 2") is None
    assert not (copy / "awg" / "awglink2.conf").exists() and not (copy / "root" / "gw-awglink2.conf").exists()
    assert "GW_BOT_TOKEN_2" not in (copy / "state" / "env").read_text(encoding="utf-8")
    assert {s["slot"]: s["case"] for s in report["slots"]} == {1: "same", 2: "removed"}
    assert _one(res, "SELECT name FROM devices WHERE id = ?", gw2) == "Pi4", "устройство снятого слота пропало"


def test_the_settings_copy_of_a_live_slot_comes_with_an_offer_and_current_traffic(world, tmp_path):
    world.db.set_state("gwkeep_1", json.dumps({"v": 1, "at": "A", "hash": "copy", "traffic": {"rx": 1}}))
    copy = world.take_copy(tmp_path / "copy")
    world.db.set_state("gwkeep_1", json.dumps({"v": 1, "at": "B", "hash": "cur", "traffic": {"rx": 9}}))
    world.run(copy)
    keep = json.loads(_one(str(copy / "state" / "bot.db"), "SELECT value FROM server_state WHERE key = 'gwkeep_1'"))
    assert keep["hash"] == "copy" and keep["offer"] == "restore" and keep["traffic"] == {"rx": 9}


# ── другая линия: новый сервер ───────────────────────────────────────────────

@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """Чистый сервер: база только что создана установщиком, профиль админа,
    свежий [Interface], другая подсеть и порт, другой токен бота."""
    from awgbot.domain.services import Services
    from awgbot.infra.db import Database
    db = Database(str(tmp_path / "fresh.db")); db.init_schema()
    s = Services(db)
    w = World(s, tmp_path / "new", prefix="10.20.1", host="new.example.org", port=40000, chport=8000,
              server_key="DUMMY-FRESH-KEY", bot_token="DUMMY-BOT-NEW")
    created = s.create_client("Админ", 0, "year", 0)
    assert s.activate_client(created.invite_code, ADMIN).ok
    w.write_client_conf()
    yield w
    db.close()


def test_a_new_server_takes_everything_from_the_copy_but_host_facts(world, fresh, tmp_path, monkeypatch):
    copy = world.take_copy(tmp_path / "copy")
    monkeypatch.setattr(config, "AWG_DIR", str(fresh.awg))
    old_files = _copy_files(copy)
    report = fresh.run(copy)
    assert report["line"] == "clean" and report["empty"] is True and report["adopt"] == ["awglink"]
    assert _copy_files(copy) == old_files, "линки и клиентский конфиг — не из бэкапа"
    import yaml
    app = yaml.safe_load((copy / "state" / "conf" / "app.yaml").read_text(encoding="utf-8"))
    assert app["network"]["subnet_cidr"] == "10.8.1.0/24" and app["network"]["server_port"] == 51820
    assert app["routing"]["link_channel_port"] == 8787
    assert app["network"]["server_host"] == "new.example.org", "адрес сервера — факт хоста"
    env = (copy / "state" / "env").read_text(encoding="utf-8")
    assert "BOT_TOKEN=DUMMY-BOT-NEW" in env and f"ADMIN_ID={ADMIN}" in env and "GW_BOT_TOKEN=DUMMY-GWBOT-1" in env
    res = str(copy / "state" / "bot.db")
    assert _one(res, "SELECT device_id FROM gateways WHERE id = 1") == world.gw1
    assert _one(res, "SELECT value FROM server_state WHERE key = 'db_line'") == world.db.get_state("db_line")
    assert {s["case"] for s in report["slots"]} == {"restored"}


def test_on_a_new_server_a_subscription_that_expired_meanwhile_is_expired_without_a_second_notice(
        world, fresh, tmp_path, monkeypatch):
    past = timeutil.to_iso(timeutil.shift(timeutil.now(), -86400))
    soon = timeutil.to_iso(timeutil.shift(timeutil.now(), 3 * 86400))
    _sql(world.db, "UPDATE client_subscription SET period_end = ?, status = 'active', notified_thresholds = '' "
                   "WHERE client_id = ?", past, world.ivan.id)
    olga = world.s.create_client("Ольга", 1, "year", 0)
    assert world.s.activate_client(olga.invite_code, 3003).ok
    year_ago = timeutil.to_iso(timeutil.shift(timeutil.now(), -362 * 86400))
    _sql(world.db, "UPDATE client_subscription SET period_start = ?, period_end = ?, notified_thresholds = '' "
                   "WHERE client_id = ?", year_ago, soon, olga.client_id)
    copy = world.take_copy(tmp_path / "copy")
    report = fresh.run(copy)
    res = str(copy / "state" / "bot.db")
    assert report["expired"] == 1
    assert _one(res, "SELECT status FROM client_subscription WHERE client_id = ?", world.ivan.id) == "expired"
    assert _one(res, "SELECT block_reason FROM clients WHERE id = ?", world.ivan.id) & int(ClientBlock.EXPIRY)
    phone_addr = world.db.get_device(world.phone).address
    assert phone_addr in report["blocked"], "DROP истёкшему не встанет до подъёма"
    assert _one(res, "SELECT notified_thresholds FROM client_subscription WHERE client_id = ?", olga.client_id), \
        "пересечённые пороги не помечены"
    from awgbot.domain.services import Services
    from awgbot.infra.db import Database
    d = Database(res)
    try:
        assert Services(d).check_expiry() == [], "после пересчёта задача сроков прислала уведомления снова"
    finally:
        d.close()


def test_on_a_new_server_counters_over_the_limit_block_this_month(world, fresh, tmp_path):
    _sql(world.db, "UPDATE device_traffic SET traffic_limit = 100, traffic_rx_month = 150 WHERE device_id = ?",
         world.phone)
    world.db.set_state("last_monthly_reset", timeutil.now().strftime("%Y-%m"))
    copy = world.take_copy(tmp_path / "copy")
    report = fresh.run(copy)
    res = str(copy / "state" / "bot.db")
    assert _one(res, "SELECT block_reason FROM devices WHERE id = ?", world.phone) & int(DeviceBlock.TRAFFIC_USER)
    assert world.db.get_device(world.phone).address in report["blocked"]


def test_same_line_takes_the_current_block_bits(world, tmp_path):
    copy = world.take_copy(tmp_path / "copy")
    _sql(world.db, "UPDATE devices SET block_reason = ? WHERE id = ?", int(DeviceBlock.TRAFFIC_USER), world.phone)
    report = world.run(copy)
    assert _one(str(copy / "state" / "bot.db"), "SELECT block_reason FROM devices WHERE id = ?", world.phone) \
        == int(DeviceBlock.TRAFFIC_USER)
    assert report["blocked"] == [world.db.get_device(world.phone).address]


# ── другая линия: чужой сервер и нечитаемая база ─────────────────────────────

def test_a_foreign_copy_keeps_the_live_slots_and_names_what_disappears(world, fresh, tmp_path, monkeypatch):
    copy = world.take_copy(tmp_path / "copy")
    # на текущем сервере — свои профили и свой шлюз в слоте 1; адрес устройства
    # шлюза совпал с адресом телефона из бэкапа
    s = fresh.s
    piotr = s.create_client("Пётр", 2, "year", 0)
    assert s.activate_client(piotr.invite_code, 4004).ok
    s.add_device(piotr.client_id, "Пётр-телефон")
    admin_id = _one(fresh.db.path, "SELECT id FROM clients WHERE tg_id = ?", ADMIN)
    pi = s.add_device(admin_id, "Pi-сейчас").device_id
    _sql(fresh.db, "UPDATE devices SET address = ? WHERE id = ?", world.db.get_device(world.phone).address, pi)
    fresh.slot(1, pi, "GW-FRESH", "VPS-FRESH")
    fresh.write_client_conf()
    monkeypatch.setattr(config, "AWG_DIR", str(fresh.awg))
    from awgbot.domain.backup import plan as plan_mod
    meta = json.loads((copy / "state" / "backup-meta.json").read_text(encoding="utf-8"))
    p = plan_mod.plan_for_dir(meta, str(copy), cur_db=fresh.db.path, conf_dir=str(fresh.conf),
                              awg_dir=str(fresh.awg), root_dir=str(fresh.gwroot))
    assert p.line == "foreign" and p.replaced == (1, 1)
    report = fresh.run(copy)
    assert report["line"] == "foreign"
    res = str(copy / "state" / "bot.db")
    dev = _one(res, "SELECT d.public_key, d.address FROM gateways g JOIN devices d ON d.id = g.device_id WHERE g.id = 1")
    assert dev == (fresh.db.get_device(pi).public_key, fresh.db.get_device(pi).address)
    assert (copy / "awg" / "awglink.conf").read_bytes() == (fresh.awg / "awglink.conf").read_bytes()
    assert report["lost"] == [{"device": "Телефон", "client": "Иван", "address": world.db.get_device(world.phone).address}]
    assert _one(res, "SELECT id FROM clients WHERE name = 'Пётр'") is None
    conf = (copy / "awg" / "awg0.conf").read_text(encoding="utf-8")
    assert fresh.db.get_device(pi).public_key in conf and world.db.get_device(world.phone).public_key not in conf


def test_an_unreadable_current_database_keeps_slots_with_link_files(world, services, tmp_path, monkeypatch):
    gw2 = services.add_device(world.admin.id, "Pi4").device_id
    world.slot(2, gw2)
    copy = world.take_copy(tmp_path / "copy")
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database at all")
    (world.awg / "awglink2.conf").unlink(); (world.gwroot / "gw-awglink2.conf").unlink()
    world.link(1, "GWPUB1-LIVE", "VPSPUB1-LIVE")
    from awgbot.domain.backup import merge
    report = merge.run(str(copy), cur_db=str(broken), conf_dir=str(world.conf), env_path=str(world.env),
                       awg_dir=str(world.awg), root_dir=str(world.gwroot), client_if="awg0")
    assert report["line"] == "unreadable"
    assert {s["slot"]: s["case"] for s in report["slots"]} == {1: "rekeyed", 2: "removed"}
    assert (copy / "awg" / "awglink.conf").read_bytes() == (world.awg / "awglink.conf").read_bytes()
    assert not (copy / "awg" / "awglink2.conf").exists()
    assert _one(str(copy / "state" / "bot.db"), "SELECT id FROM gateways WHERE id = 2") is None


def test_the_plan_names_a_different_server_address(world, tmp_path):
    copy = world.take_copy(tmp_path / "copy")
    (world.conf / "app.yaml").write_text(APP.format(prefix="10.8.1", host="moved.example.org", port=51820,
                                                    chport=8787, day=1), encoding="utf-8")
    from awgbot.domain.backup import plan as plan_mod
    from awgbot.domain.notices import backup as nb
    meta = json.loads((copy / "state" / "backup-meta.json").read_text(encoding="utf-8"))
    p = plan_mod.plan_for_dir(meta, str(copy), cur_db=world.db.path, conf_dir=str(world.conf),
                              awg_dir=str(world.awg), root_dir=str(world.gwroot))
    assert (p.host_copy, p.host_cur) == ("vpn.example.org", "moved.example.org")
    text = nb.plain(nb.restore_plan(p))
    assert "В бэкапе адрес сервера vpn.example.org, сейчас — moved.example.org" in text


def test_a_merge_refusal_leaves_the_live_state_untouched(world, tmp_path):
    copy = world.take_copy(tmp_path / "copy")
    (copy / "state" / "bot.db").unlink()
    before, env = world.files(), world.env.read_bytes()
    from awgbot.domain.backup import merge
    with pytest.raises(merge.MergeError):
        world.run(copy)
    assert world.files() == before and world.env.read_bytes() == env
    assert not (copy / merge.REPORT_NAME).exists()


def test_the_cli_tool_merges_and_plans(world, tmp_path, monkeypatch, capsys):
    copy = world.take_copy(tmp_path / "copy")
    monkeypatch.setattr(config, "DB_PATH", Path(world.db.path))
    monkeypatch.setattr(config, "CONF_DIR", world.conf)
    monkeypatch.setattr(config, "ENV_PATH", world.env)
    from tools import restore_merge
    assert restore_merge.main(["--plan", str(copy)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("♻️ Восстановить бэкап от ") and "<" not in out, out
    from awgbot.domain.backup import merge
    blocked = []
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "block_ip", lambda ip: blocked.append(ip))
    report = merge.run(str(copy), cur_db=world.db.path, conf_dir=str(world.conf), env_path=str(world.env),
                       awg_dir=str(world.awg), root_dir=str(world.gwroot), client_if="awg0")
    report_path = copy / merge.REPORT_NAME
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data == report
    data["blocked"] = ["10.8.1.77"]
    report_path.write_text(json.dumps(data), encoding="utf-8")
    assert restore_merge.main(["--apply-blocks", str(report_path)]) == 0 and blocked == ["10.8.1.77"]
    assert os.path.exists(copy / "state" / "bot.db")
