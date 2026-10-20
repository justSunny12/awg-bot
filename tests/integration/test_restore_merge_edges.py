"""
Слияние бэкапа с текущим, частные случаи: пары переезда, обвязка и DNS на той же
линии, факты хоста и фраза на другой, сопоставление по времени создания, разные
схемы, мёртвые устройства слота, снятый и назначенный заново слот, план, изменившийся
после вопроса.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.core.blocks import DeviceBlock
from awgbot.util import timeutil

from tests.integration._restore_merge_common import ADMIN, _one, _sql, make_fresh, make_world

pytestmark = pytest.mark.integration


@pytest.fixture()
def world(services, make_active_client, monkeypatch, tmp_path, fake_awg):
    return make_world(services, make_active_client, monkeypatch, tmp_path)


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """Чистый сервер: база только что создана установщиком, профиль админа,
    свежий [Interface], другая подсеть и порт, другой токен бота."""
    w, db = make_fresh(tmp_path)
    yield w
    db.close()


def test_on_a_new_server_a_migration_pair_is_judged_by_the_sum_of_both_rows(world, fresh, tmp_path, services):
    """Пара переезда: расход — сумма обеих строк против лимита пары, старая
    строка отдельно не судится (как check_traffic_limits)."""
    twin = services.add_device(world.ivan.id, "Телефон-двойник").device_id
    _sql(world.db, "UPDATE devices SET twin_of = ? WHERE id = ?", world.phone, twin)
    for dev, rx in ((world.phone, 60), (twin, 60)):
        _sql(world.db, "UPDATE device_traffic SET traffic_limit = 100, traffic_rx_month = ? WHERE device_id = ?",
             rx, dev)
    world.db.set_state("last_monthly_reset", timeutil.now().strftime("%Y-%m"))
    copy = world.take_copy(tmp_path / "copy")
    fresh.run(copy)
    res = str(copy / "state" / "bot.db")
    for dev in (world.phone, twin):
        assert _one(res, "SELECT block_reason FROM devices WHERE id = ?", dev) & int(DeviceBlock.TRAFFIC_USER), dev
    markers = _one(res, "SELECT traffic_notified FROM client_quota WHERE client_id = ?", world.ivan.id)
    assert f"dev_over:{twin}" in markers and f"dev_over:{world.phone}" not in markers


# ── правки по ревью ──────────────────────────────────────────────────────────

def test_same_line_keeps_the_plumbing_and_dns_handed_out_to_devices(world, tmp_path):
    import yaml
    app = world.conf / "app.yaml"
    app.write_text(app.read_text(encoding="utf-8") + 'client_config:\n  dns1: "10.8.1.1"\n  admin_dns: "10.255.53.53"\n'
                   '  dns2: "10.8.1.1"\n', encoding="utf-8")
    app.write_text(app.read_text(encoding="utf-8").replace("routing:\n", 'routing:\n  gw_interface: ""\n'),
                   encoding="utf-8")
    copy = world.take_copy(tmp_path / "copy")
    app.write_text(app.read_text(encoding="utf-8").replace('gw_interface: ""', 'gw_interface: "awglink"')
                   .replace('dns1: "10.8.1.1"', 'dns1: "198.51.100.53"'), encoding="utf-8")
    world.run(copy)
    got = yaml.safe_load((copy / "state" / "conf" / "app.yaml").read_text(encoding="utf-8"))
    assert got["routing"]["gw_interface"] == "awglink", "обвязка усыплена бэкапом до развёртывания"
    assert got["client_config"]["dns1"] == "198.51.100.53"


def test_other_line_keeps_host_facts_and_takes_the_phrase_only_if_there_is_none(world, fresh, tmp_path):
    world.db.set_state("host_boot_id", "old-boot"); world.db.set_state("service_started_at", "old")
    world.db.set_state("backup_passphrase", "copy phrase 1"); world.db.set_state("updates_muted", "1")
    copy = world.take_copy(tmp_path / "copy")
    fresh.db.set_state("host_boot_id", "new-boot"); fresh.db.set_state("service_started_at", "new")
    fresh.run(copy)
    res = str(copy / "state" / "bot.db")
    state = dict(sqlite3.connect(res).execute("SELECT key, value FROM server_state").fetchall())
    assert (state["host_boot_id"], state["service_started_at"]) == ("new-boot", "new"), "ложное «хост перезагружен»"
    assert state["backup_passphrase"] == "copy phrase 1", "на новом сервере без фразы функция бэкапов пропала"
    assert "updates_muted" not in state, "«не уведомлять» — факт текущей установки"
    copy2 = world.take_copy(tmp_path / "copy2")
    fresh.db.set_state("backup_passphrase", "current phrase")
    fresh.run(copy2)
    assert _one(str(copy2 / "state" / "bot.db"), "SELECT value FROM server_state WHERE key = 'backup_passphrase'") \
        == "current phrase"


def test_traffic_of_a_live_slot_without_a_current_block_does_not_come_from_the_copy(world, tmp_path):
    world.db.set_state("gwkeep_1", json.dumps({"v": 1, "hash": "copy", "at": "A", "items": {},
                                               "traffic": {"month": "2026-10", "rx": 9, "tx": 9}}))
    copy = world.take_copy(tmp_path / "copy")
    world.db.set_state("gwkeep_1", "")
    world.run(copy)
    keep = json.loads(_one(str(copy / "state" / "bot.db"), "SELECT value FROM server_state WHERE key = 'gwkeep_1'"))
    assert keep["offer"] == "restore" and "traffic" not in keep


def test_same_line_matches_entities_by_id_and_creation_time(world, tmp_path):
    copy = world.take_copy(tmp_path / "copy")
    # строка с тем же id, но другая (создана заново) — настройки бэкапа к ней не относятся
    _sql(world.db, "UPDATE clients SET name = 'Другой', created_at = '2026-10-17T00:00:00+03:00' WHERE id = ?",
         world.ivan.id)
    _sql(world.db, "UPDATE devices SET name = 'Другое', created_at = '2026-10-17T00:00:00+03:00' WHERE id = ?",
         world.phone)
    world.run(copy)
    res = str(copy / "state" / "bot.db")
    assert _one(res, "SELECT name FROM clients WHERE id = ?", world.ivan.id) == "Другой"
    assert _one(res, "SELECT name FROM devices WHERE id = ?", world.phone) == "Другое"


def test_other_line_copies_only_columns_both_schemas_have(world, fresh, tmp_path, monkeypatch):
    copy = world.take_copy(tmp_path / "copy")
    s = fresh.s
    pi = s.add_device(_one(fresh.db.path, "SELECT id FROM clients WHERE tg_id = ?", ADMIN), "Pi").device_id
    fresh.slot(1, pi, "GW-F", "VPS-F")
    fresh.write_client_conf()
    _sql(fresh.db, "ALTER TABLE devices ADD COLUMN extra_future TEXT DEFAULT 'x'")
    _sql(fresh.db, "ALTER TABLE gateways ADD COLUMN extra_future TEXT DEFAULT 'x'")
    monkeypatch.setattr(config, "AWG_DIR", str(fresh.awg))
    report = fresh.run(copy)
    assert {s["slot"]: s["case"] for s in report["slots"]}[1] in ("device", "rekeyed")
    assert _one(str(copy / "state" / "bot.db"), "SELECT d.public_key FROM gateways g JOIN devices d "
                                                 "ON d.id = g.device_id WHERE g.id = 1") == fresh.db.get_device(pi).public_key


def _renumber_admin(db: Path, new_id: int) -> int:
    """Админ копии — под другим id, чем на новом сервере (у разных установок id расходятся)."""
    con = sqlite3.connect(str(db))
    try:
        old = con.execute("SELECT id FROM clients WHERE tg_id = ?", (ADMIN,)).fetchone()[0]
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for col in [r[1] for r in con.execute(f"PRAGMA table_info({table})")]:
                if col in ("client_id", "holder_client_id"):
                    con.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (new_id, old))
        con.execute("UPDATE clients SET id = ? WHERE id = ?", (new_id, old))
        con.commit()
    finally:
        con.close()
    return new_id


def test_other_line_drops_the_dead_slot_device_and_puts_a_twin_into_its_interface(world, fresh, tmp_path,
                                                                                  monkeypatch):
    copy = world.take_copy(tmp_path / "copy")
    old_gw_pub = world.db.get_device(world.gw1).public_key
    s = fresh.s
    copy_admin = _renumber_admin(copy / "state" / "bot.db", 77)
    admin = _one(fresh.db.path, "SELECT id FROM clients WHERE tg_id = ?", ADMIN)
    pi = s.add_device(admin, "Pi").device_id
    twin = s.add_device(admin, "Pi-двойник").device_id
    _sql(fresh.db, "UPDATE devices SET twin_of = ?, iface = 'awg1' WHERE id = ?", pi, twin)
    _sql(fresh.db, "UPDATE devices SET holder_client_id = ? WHERE id = ?", admin, pi)
    fresh.slot(1, pi, "GW-F", "VPS-F")
    fresh.write_client_conf()
    twin_pub = fresh.db.get_device(twin).public_key
    (fresh.awg / "awg1.conf").write_text(f"[Interface]\nPrivateKey = DUMMY-MIG\n\n[Peer]\nPublicKey = {twin_pub}\n"
                                         "PresharedKey = DUMMY\nAllowedIPs = 10.9.1.5/32\n", encoding="utf-8")
    (copy / "awg" / "awg1.conf").write_text("[Interface]\nPrivateKey = DUMMY-MIG-COPY\n", encoding="utf-8")
    monkeypatch.setattr(config, "AWG_DIR", str(fresh.awg))
    fresh.run(copy)
    conf0 = (copy / "awg" / "awg0.conf").read_text(encoding="utf-8")
    conf1 = (copy / "awg" / "awg1.conf").read_text(encoding="utf-8")
    assert old_gw_pub not in conf0, "мёртвый пир прежнего устройства слота остался"
    assert twin_pub in conf1 and twin_pub not in conf0, "двойник переезда — не в своём интерфейсе"
    res = str(copy / "state" / "bot.db")
    assert _one(res, "SELECT id FROM devices WHERE public_key = ?", old_gw_pub) is None
    pi_pub = fresh.db.get_device(pi).public_key
    assert admin != copy_admin, "у админов совпали id — проверка держателя ничего не докажет"
    assert _one(res, "SELECT holder_client_id FROM devices WHERE public_key = ?", pi_pub) == copy_admin, \
        "держатель не переведён на админа копии"


def test_a_slot_removed_and_reassigned_after_the_copy_gets_no_offer_of_the_removed_settings(world, services,
                                                                                            tmp_path):
    world.db.set_state("gwkeep_1", json.dumps({"v": 1, "hash": "old-device", "at": "A", "items": {}}))
    copy = world.take_copy(tmp_path / "copy")
    services._run_link_script = lambda mode, env=None: None
    services.gateway_remove(1)
    other = services.add_device(world.admin.id, "Pi-новый").device_id
    world.slot(1, other, "GWPUB1-B", "VPSPUB1-B")
    world.db.set_state("gwkeep_1", json.dumps({"v": 1, "hash": "new-device", "at": "B", "items": {}}))
    live = world.files()
    report = world.run(copy)
    assert [(s["slot"], s["case"]) for s in report["slots"]] == [(1, "removed"), (1, "added")]
    keep = json.loads(_one(str(copy / "state" / "bot.db"), "SELECT value FROM server_state WHERE key = 'gwkeep_1'"))
    assert keep["hash"] == "new-device" and "offer" not in keep, "настройки снятого предложены новому"
    assert (copy / "awg" / "awglink.conf").read_bytes() == live["awg/awglink.conf"]


def test_a_plan_that_changed_since_the_question_is_refused_and_nothing_changes(world, services, tmp_path):
    from awgbot.domain.backup import merge, meta, plan
    gw2 = services.add_device(world.admin.id, "Pi4").device_id
    world.slot(2, gw2)
    copy = world.take_copy(tmp_path / "copy")
    m = json.loads((copy / meta.META_NAME).read_text(encoding="utf-8"))
    asked = plan.plan_for_dir(m, str(copy), cur_db=world.db.path, conf_dir=str(world.conf),
                              awg_dir=str(world.awg), root_dir=str(world.gwroot)).to_dict()
    world.s._run_link_script = lambda mode, env=None: None
    world.s.gateway_remove(2)                      # между вопросом и остановкой сняли шлюз
    before, env = world.files(), world.env.read_bytes()
    with pytest.raises(merge.MergeError, match="изменились шлюзы или база"):
        merge.run(str(copy), cur_db=world.db.path, conf_dir=str(world.conf), env_path=str(world.env),
                  awg_dir=str(world.awg), root_dir=str(world.gwroot), client_if="awg0", expect=asked)
    assert world.files() == before and world.env.read_bytes() == env
    assert not (copy / merge.REPORT_NAME).exists()
    again = plan.plan_for_dir(m, str(copy), cur_db=world.db.path, conf_dir=str(world.conf),
                              awg_dir=str(world.awg), root_dir=str(world.gwroot)).to_dict()
    report = merge.run(str(copy), cur_db=world.db.path, conf_dir=str(world.conf), env_path=str(world.env),
                       awg_dir=str(world.awg), root_dir=str(world.gwroot), client_if="awg0", expect=again)
    assert {s["slot"]: s["case"] for s in report["slots"]} == {1: "same", 2: "removed"}
