"""
Общее для тестов слияния бэкапа с текущим: сервер AWG во временном каталоге
(база сервисов, клиентский конфиг, линки слотов, app.yaml, env) и его сборка.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from awgbot.core import config

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



def make_world(services, make_active_client, monkeypatch, tmp_path) -> World:
    """Живой сервер: админ со слотом 1, Иван с телефоном и ноутбуком."""
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


def make_fresh(tmp_path):
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
    return w, db
