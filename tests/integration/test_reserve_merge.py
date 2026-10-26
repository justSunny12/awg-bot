"""Слияние при возврате пользователей с резерва (domain/backup/merge/reserve.py,
policy.py) на трёх настоящих базах: снимок `base`, живая база основы `cur`,
реплика бота основы на резерве `rep`. Итог строится в `cur`.

Цена ошибки: потерянный при слиянии профиль или устройство — человек заходил
через резерв неделю, вернулась основа, а его профиля нет; перенос без смены
`id` поверх занятого — чужое устройство получает чужие счётчики и лимиты;
счётчики не по формуле `cur + (rep − base)` — трафик либо удваивается (лимит
срабатывает рано), либо теряется (лимит не срабатывает); маски блокировок
не побитно — снятая на резерве блокировка возвращается или поставленная на
основе пропадает; новая колонка без правила — слияние молча теряет её
значения на следующей версии.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from awgbot.domain.backup.merge import policy as P
from awgbot.domain.backup.merge import reserve as M
from awgbot.infra.db import Database
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

BIT_A, BIT_B, BIT_C = 1, 2, 4


def mk_client(db, name, tg, *, end="2099-01-01T00:00:00+03:00") -> int:
    cid = db.create_client(name, 3, "2026-01-01T00:00:00+03:00", end, f"INV-{name}")
    db.activate_client(cid, tg)
    return cid


def mk_dev(db, cid, name, addr, pub=None) -> int:
    return db.create_device(cid, name, pub or f"DUMMY-pub-{name}", "DUMMY-psk", addr)


class Trio:
    """Три базы: снимок, основа, реплика. Сначала `seed(db)` на снимке, затем
    базы расходятся копией; правки — через `.cur()` и `.rep()`."""

    def __init__(self, tmp_path: Path, seed):
        self.paths = {k: str(tmp_path / f"{k}.db") for k in ("base", "cur", "rep")}
        db = Database(self.paths["base"])
        db.init_schema()
        self.seeded = seed(db)
        db.close()
        for k in ("cur", "rep"):
            src = sqlite3.connect(self.paths["base"])
            dst = sqlite3.connect(self.paths[k])
            src.backup(dst)
            dst.close()
            src.close()
        self._open: list[Database] = []

    def _db(self, which) -> Database:
        db = Database(self.paths[which])
        self._open.append(db)
        return db

    def cur(self) -> Database:
        return self._db("cur")

    def rep(self) -> Database:
        return self._db("rep")

    def sql(self, which, query, args=()):
        con = sqlite3.connect(self.paths[which])
        try:
            rows = con.execute(query, args).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    def merge(self) -> M.Report:
        for db in self._open:
            db.close()
        self._open.clear()
        return M.merge(self.paths["cur"], self.paths["base"], self.paths["rep"])

    def result(self, query, args=()):
        return self.sql("cur", query, args)


@pytest.fixture()
def trio(tmp_path):
    def build(seed=None):
        return Trio(tmp_path, seed or _basic)
    return build


def _basic(db):
    """Снимок: профили «Аня» и «Борис», по одному устройству у каждого."""
    a, b = mk_client(db, "Аня", 2001), mk_client(db, "Борис", 2002)
    da, dbb = mk_dev(db, a, "Телефон", "10.8.1.2"), mk_dev(db, b, "Ноутбук", "10.8.1.3")
    return {"a": a, "b": b, "da": da, "db": dbb}


# ── строки: вставка, удаление ────────────────────────────────────────────────

def test_a_profile_and_a_device_created_on_the_reserve_arrive_with_their_own_rows(trio):
    t = trio()
    rep = t.rep()
    cid = mk_client(rep, "Вера", 2003)
    did = mk_dev(rep, cid, "Планшет", "10.8.1.4")
    rep.add_routing_domain(cid, "example.org")
    rpt = t.merge()
    row = t.result("SELECT c.id, c.tg_id, d.id, d.address, d.client_id FROM clients c JOIN devices d ON d.client_id = c.id "
                   "WHERE c.name = 'Вера'")
    assert row == [(cid, 2003, did, "10.8.1.4", cid)], "ids свободны — переносятся как есть"
    assert t.result("SELECT COUNT(*) FROM device_traffic WHERE device_id = ?", (did,)) == [(1,)]
    assert t.result("SELECT COUNT(*) FROM client_subscription WHERE client_id = ?", (cid,)) == [(1,)]
    assert t.result("SELECT COUNT(*) FROM client_quota WHERE client_id = ?", (cid,)) == [(1,)]
    assert t.result("SELECT domain FROM client_routing_domains WHERE client_id = ?", (cid,)) == [("example.org",)]
    assert rpt.inserted["clients"] == 1 and rpt.inserted["devices"] == 1 and not rpt.remapped and not rpt.lost


def test_rows_created_on_both_sides_with_the_same_id_get_a_new_id_for_the_reserves_one_with_all_references(trio):
    """Основа успела завести профиль с тем же id, пока резерв заводил свой: оба живут,
    ссылки резерва переписаны на новый id."""
    t = trio()
    cur, rep = t.cur(), t.rep()
    cc = mk_client(cur, "Основа-новый", 2100)
    cd = mk_dev(cur, cc, "Основа-устройство", "10.8.1.10")
    rc = mk_client(rep, "Резерв-новый", 2200)
    rd = mk_dev(rep, rc, "Резерв-устройство", "10.8.1.20")
    assert (cc, cd) == (rc, rd), "условие теста: оба новых получили одинаковые id"
    rep.add_routing_domain(rc, "example.org")
    rep.update_device_fields(rd, traffic_limit=777)
    rpt = t.merge()
    names = dict(t.result("SELECT name, id FROM clients"))
    assert names["Основа-новый"] == cc, "строка основы переехала"
    new_c = names["Резерв-новый"]
    assert new_c != cc
    devs = dict(t.result("SELECT name, id FROM devices"))
    assert devs["Основа-устройство"] == cd and devs["Резерв-устройство"] != cd
    new_d = devs["Резерв-устройство"]
    assert t.result("SELECT client_id FROM devices WHERE id = ?", (new_d,)) == [(new_c,)], "устройство резерва осталось у чужого профиля"
    assert t.result("SELECT traffic_limit FROM device_traffic WHERE device_id = ?", (new_d,)) == [(777,)]
    assert t.result("SELECT COUNT(*) FROM device_traffic WHERE device_id = ?", (cd,)) == [(1,)], "счётчики устройства основы затёрты"
    assert t.result("SELECT domain FROM client_routing_domains WHERE client_id = ?", (new_c,)) == [("example.org",)]
    assert t.result("SELECT COUNT(*) FROM client_subscription WHERE client_id = ?", (new_c,)) == [(1,)]
    assert rpt.remapped == {"clients": 1, "devices": 1}


def test_a_device_removed_on_the_reserve_is_removed_with_its_rows(trio):
    t = trio()
    rep = t.rep()
    rep.delete_device(t.seeded["da"])
    rpt = t.merge()
    assert t.result("SELECT COUNT(*) FROM devices WHERE id = ?", (t.seeded["da"],)) == [(0,)]
    assert t.result("SELECT COUNT(*) FROM device_traffic WHERE device_id = ?", (t.seeded["da"],)) == [(0,)]
    assert t.result("SELECT COUNT(*) FROM devices WHERE id = ?", (t.seeded["db"],)) == [(1,)], "соседнее устройство не тронуто"
    assert rpt.deleted["devices"] == 1


def test_a_profile_removed_on_the_reserve_takes_its_devices_and_rows_with_it(trio):
    t = trio()
    t.rep().delete_client(t.seeded["a"])
    t.merge()
    assert t.result("SELECT COUNT(*) FROM clients WHERE id = ?", (t.seeded["a"],)) == [(0,)]
    assert t.result("SELECT COUNT(*) FROM devices WHERE client_id = ?", (t.seeded["a"],)) == [(0,)]
    assert t.result("SELECT COUNT(*) FROM client_subscription WHERE client_id = ?", (t.seeded["a"],)) == [(0,)]
    assert t.result("SELECT COUNT(*) FROM device_traffic WHERE device_id = ?", (t.seeded["da"],)) == [(0,)]
    assert t.result("SELECT COUNT(*) FROM clients WHERE id = ?", (t.seeded["b"],)) == [(1,)]


def test_a_device_the_main_removed_after_the_snapshot_is_not_brought_back(trio):
    t = trio()
    t.cur().delete_device(t.seeded["da"])
    t.rep().update_device_fields(t.seeded["da"], name="Переименован на резерве")
    t.merge()
    assert t.result("SELECT COUNT(*) FROM devices WHERE id = ?", (t.seeded["da"],)) == [(0,)]


# ── поля ─────────────────────────────────────────────────────────────────────

def test_a_field_changed_on_the_reserve_takes_the_replica_value_even_if_the_main_changed_it_too(trio):
    t = trio()
    t.cur().update_client_fields(t.seeded["a"], name="Аня (основа)", device_limit=5)
    t.rep().update_client_fields(t.seeded["a"], name="Аня (резерв)", period_end="2030-05-05T00:00:00+03:00")
    t.merge()
    name, limit, end = t.result("SELECT c.name, c.device_limit, s.period_end FROM clients c JOIN client_subscription s "
                                "ON s.client_id = c.id WHERE c.id = ?", (t.seeded["a"],))[0]
    assert name == "Аня (резерв)", "изменение на резерве позже по времени и главнее"
    assert limit == 5, "поле, которое резерв не менял, остаётся значением основы"
    assert end == "2030-05-05T00:00:00+03:00"


def test_host_columns_stay_with_the_main(trio):
    """Закрытый ключ устройства в реплику не едет (там NULL) и перетереть ключ основы не может."""
    t = trio()
    t.cur().update_device_fields(t.seeded["da"], private_key="DUMMY-private")
    t.merge()
    assert t.result("SELECT private_key FROM devices WHERE id = ?", (t.seeded["da"],)) == [("DUMMY-private",)]


@pytest.mark.parametrize("base, cur, rep, want", [
    (BIT_B, BIT_B | BIT_C, BIT_B | BIT_A, BIT_A | BIT_B | BIT_C),   # резерв поставил A, основа — C: оба
    (BIT_B, BIT_B | BIT_C, 0, BIT_C),                                  # резерв снял B: C основы остался
    (0, BIT_C, 0, BIT_C),                                              # резерв не менял: как у основы
    (BIT_A, 0, BIT_A, 0),                                              # резерв не менял, основа сняла
    (BIT_A | BIT_B, BIT_A | BIT_B, BIT_A, BIT_A),                      # резерв снял только B
])
def test_block_masks_merge_bit_by_bit(trio, base, cur, rep, want):
    def seed(db):
        cid = mk_client(db, "Аня", 2001)
        did = mk_dev(db, cid, "Телефон", "10.8.1.2")
        db.client_mask_apply(cid, or_bits=base)
        db.device_mask_apply(did, or_bits=base)
        return {"c": cid, "d": did}
    t = trio(seed)
    for which, mask in (("cur", cur), ("rep", rep)):
        t.sql(which, "UPDATE clients SET block_reason = ?", (mask,))
        t.sql(which, "UPDATE devices SET block_reason = ?", (mask,))
    t.merge()
    assert t.result("SELECT block_reason FROM clients WHERE id = ?", (t.seeded["c"],)) == [(want,)], "маска профиля"
    assert t.result("SELECT block_reason FROM devices WHERE id = ?", (t.seeded["d"],)) == [(want,)], "маска устройства"


# ── счётчики ─────────────────────────────────────────────────────────────────

COUNTER_COLS = ("traffic_rx_month", "traffic_tx_month", "traffic_rx_period", "traffic_tx_period", "rf_rx_month", "rf_tx_month")


def _counters(t, which, did, **vals):
    sets = ", ".join(f"{k} = {int(v)}" for k, v in vals.items())
    t.sql(which, f"UPDATE device_traffic SET {sets} WHERE device_id = ?", (did,))


def test_counters_are_current_plus_what_the_reserve_added_over_the_snapshot(trio):
    t = trio()
    did = t.seeded["da"]
    base = dict(zip(COUNTER_COLS, (100, 200, 300, 400, 10, 20)))
    for w in ("base", "cur", "rep"):
        _counters(t, w, did, **base)
    _counters(t, "cur", did, **{c: v + 50 for c, v in base.items()})          # основа дописала 50
    _counters(t, "rep", did, **{c: v + 30 for c, v in base.items()})          # резерв дописал 30
    t.merge()
    got = t.result(f"SELECT {', '.join(COUNTER_COLS)} FROM device_traffic WHERE device_id = ?", (did,))[0]
    assert got == tuple(v + 80 for v in base.values()), "ожидалось cur + (rep − base) для каждого счётчика"


def test_counters_that_the_reserve_did_not_touch_keep_the_mains_value(trio):
    t = trio()
    did = t.seeded["da"]
    for w in ("base", "rep"):
        _counters(t, w, did, traffic_rx_month=100)
    _counters(t, "cur", did, traffic_rx_month=400)
    t.merge()
    assert t.result("SELECT traffic_rx_month FROM device_traffic WHERE device_id = ?", (did,)) == [(400,)]


def test_last_handshake_is_the_later_of_the_two(trio):
    t = trio()
    a, b = t.seeded["da"], t.seeded["db"]
    t.sql("cur", "UPDATE device_traffic SET last_handshake = 2000 WHERE device_id = ?", (a,))
    t.sql("rep", "UPDATE device_traffic SET last_handshake = 1500 WHERE device_id = ?", (a,))
    t.sql("cur", "UPDATE device_traffic SET last_handshake = 1000 WHERE device_id = ?", (b,))
    t.sql("rep", "UPDATE device_traffic SET last_handshake = 3000 WHERE device_id = ?", (b,))
    t.merge()
    assert t.result("SELECT device_id, last_handshake FROM device_traffic WHERE device_id IN (?, ?) ORDER BY device_id",
                    (a, b)) == [(a, 2000), (b, 3000)]


def test_a_month_boundary_on_the_reserve_keeps_the_replica_month_and_archives_the_mains_delta(trio):
    """На резерве прошёл месячный сброс: месячные счётчики — с реплики, а то,
    что основа накопила за прошлый месяц после снимка, не теряется, а уходит в архив прошлого месяца."""
    t = trio()
    did = t.seeded["da"]
    prev = timeutil.shift(timeutil.now().replace(day=1), -1).strftime("%Y-%m")
    for w in ("base",):
        _counters(t, w, did, traffic_rx_month=1000, traffic_tx_month=2000, rf_rx_month=100, rf_tx_month=200)
    _counters(t, "cur", did, traffic_rx_month=1300, traffic_tx_month=2500, rf_rx_month=130, rf_tx_month=260)
    _counters(t, "rep", did, traffic_rx_month=40, traffic_tx_month=50, rf_rx_month=4, rf_tx_month=5)
    t.sql("rep", "INSERT INTO traffic_monthly (device_id, client_id, month, rx, tx, rf_rx, rf_tx, archived_at) "
                 "VALUES (?, ?, ?, 1100, 2100, 110, 210, '2026-10-01T00:00:00+03:00')", (did, t.seeded["a"], prev))
    rpt = t.merge()
    assert t.result("SELECT traffic_rx_month, traffic_tx_month, rf_rx_month, rf_tx_month FROM device_traffic "
                    "WHERE device_id = ?", (did,)) == [(40, 50, 4, 5)], "новый месяц — значения реплики"
    rx, tx, rfrx, rftx = t.result("SELECT SUM(rx), SUM(tx), SUM(rf_rx), SUM(rf_tx) FROM traffic_monthly "
                                  "WHERE device_id = ? AND month = ?", (did, prev))[0]
    assert (rx, tx, rfrx, rftx) == (1100 + 300, 2100 + 500, 110 + 30, 210 + 60), \
        "прошлый месяц = итог реплики + дельта основы после снимка"
    assert rpt.month_archived == 1


def test_a_month_boundary_adds_to_an_existing_archive_row_of_the_previous_month(trio):
    t = trio()
    did = t.seeded["da"]
    prev = timeutil.shift(timeutil.now().replace(day=1), -1).strftime("%Y-%m")
    _counters(t, "base", did, traffic_rx_month=1000)
    _counters(t, "cur", did, traffic_rx_month=1300)
    _counters(t, "rep", did, traffic_rx_month=10)
    t.sql("cur", "INSERT INTO traffic_monthly (device_id, client_id, month, rx, tx, rf_rx, rf_tx, archived_at) "
                 "VALUES (?, ?, ?, 5, 6, 0, 0, '2026-10-01T00:00:00+03:00')", (did, t.seeded["a"], prev))
    t.merge()
    assert t.result("SELECT SUM(rx) FROM traffic_monthly WHERE device_id = ? AND month = ?", (did, prev)) == [(5 + 300,)]


def test_no_archive_row_is_made_when_the_main_had_no_delta(trio):
    t = trio()
    did = t.seeded["da"]
    for w in ("base", "cur"):
        _counters(t, w, did, traffic_rx_month=1000)
    _counters(t, "rep", did, traffic_rx_month=0)
    rpt = t.merge()
    assert t.result("SELECT COUNT(*) FROM traffic_monthly") == [(0,)] and rpt.month_archived == 0


# ── что остаётся основе, что дописывается, что берётся у реплики ──────────────

def test_samples_gateways_and_reserve_tables_stay_the_mains(trio):
    t = trio()
    did = t.seeded["da"]
    t.sql("base", "INSERT INTO traffic_samples VALUES (?, 10, 10, 'x')", (did,))
    t.sql("cur", "INSERT INTO traffic_samples VALUES (?, 5000, 6000, 'cur')", (did,))
    t.sql("rep", "INSERT INTO traffic_samples VALUES (?, 7, 7, 'rep')", (did,))
    t.sql("cur", "INSERT INTO rf_samples VALUES (?, 11, 12, 'cur')", (did,))
    t.sql("rep", "INSERT INTO rf_samples VALUES (?, 1, 2, 'rep')", (did,))
    t.sql("cur", "INSERT INTO gateways (id, device_id, link_if, link_port, link_cidr, created_at) "
                 "VALUES (1, ?, 'awglink', 443, '10.99.99.0/30', 'x')", (did,))
    t.sql("rep", "INSERT INTO reserve_peer (name, host, pair_if, pair_port, peer_port, pair_cidr, created_at) "
                 "VALUES ('Чужая', '198.51.100.9', 'awgpair', 1, 2, '10.99.98.0/30', 'x')")
    t.merge()
    assert t.result("SELECT last_rx, last_tx FROM traffic_samples WHERE device_id = ?", (did,)) == [(5000, 6000)]
    assert t.result("SELECT last_up, last_dn FROM rf_samples WHERE device_id = ?", (did,)) == [(11, 12)]
    assert t.result("SELECT link_if FROM gateways") == [("awglink",)], "слоты шлюзов — хостовые данные основы"
    assert t.result("SELECT COUNT(*) FROM reserve_peer") == [(0,)], "пару основы реплика не меняет"


def test_history_written_on_the_reserve_is_appended_and_the_mains_history_stays(trio):
    t = trio()
    a = t.seeded["a"]
    t.sql("cur", "INSERT INTO client_block_histories (client_id, block_reason, archived_at, close_reason) "
                 "VALUES (?, 1, '2026-10-01', 'main')", (a,))
    t.sql("rep", "INSERT INTO client_block_histories (client_id, block_reason, archived_at, close_reason) "
                 "VALUES (?, 2, '2026-10-02', 'reserve')", (a,))
    t.sql("rep", "INSERT INTO client_block_histories (client_id, block_reason, archived_at, close_reason) "
                 "VALUES (?, 4, '2026-10-03', 'reserve2')", (a,))
    t.merge()
    assert t.result("SELECT close_reason FROM client_block_histories ORDER BY close_reason") == [
        ("main",), ("reserve",), ("reserve2",)]


def test_history_of_a_moved_profile_points_to_its_new_id(trio):
    t = trio()
    cur, rep = t.cur(), t.rep()
    mk_client(cur, "Основа-новый", 2100)
    rc = mk_client(rep, "Резерв-новый", 2200)
    t.sql("rep", "INSERT INTO client_block_histories (client_id, block_reason, archived_at, close_reason) "
                 "VALUES (?, 2, '2026-10-02', 'reserve')", (rc,))
    t.merge()
    new_id = t.result("SELECT id FROM clients WHERE name = 'Резерв-новый'")[0][0]
    assert new_id != rc
    assert t.result("SELECT client_id FROM client_block_histories WHERE close_reason = 'reserve'") == [(new_id,)]


def test_the_menu_state_is_taken_from_the_replica(trio):
    t = trio()
    t.sql("cur", "INSERT INTO ui_state VALUES (2001, 111, '[]')")
    t.sql("rep", "INSERT INTO ui_state VALUES (2001, 999, '[1]')")
    t.sql("rep", "INSERT INTO ui_state VALUES (2002, 5, '[]')")
    t.merge()
    assert t.result("SELECT chat_id, nav_message_id FROM ui_state ORDER BY chat_id") == [(2001, 999), (2002, 5)]


def test_only_whitelisted_server_state_keys_changed_on_the_reserve_come_back(trio):
    t = trio()
    t.sql("base", "INSERT INTO server_state VALUES ('email_login', 'old@example.org')")
    t.sql("cur", "INSERT INTO server_state VALUES ('email_login', 'old@example.org')")
    t.sql("rep", "INSERT INTO server_state VALUES ('email_login', 'old@example.org')")
    t.sql("rep", "UPDATE server_state SET value = 'new@example.org' WHERE key = 'email_login'")
    t.sql("cur", "INSERT INTO server_state VALUES ('backup_passphrase', 'DUMMY-main')")
    t.sql("rep", "INSERT INTO server_state VALUES ('backup_passphrase', 'DUMMY-reserve')")
    t.sql("rep", "INSERT INTO server_state VALUES ('rsv_in', '{\"state\": \"serving\"}')")
    t.sql("rep", "INSERT INTO server_state VALUES ('email_check', '1')")
    t.merge()
    state = dict(t.result("SELECT key, value FROM server_state"))
    assert state["email_login"] == "new@example.org"
    assert state["email_check"] == "1", "ключ белого списка, заведённый на резерве"
    assert state["backup_passphrase"] == "DUMMY-main", "ключ вне белого списка реплика не перетирает"
    assert "rsv_in" not in state, "служебный ключ пары с резерва не возвращается"


def test_a_whitelisted_key_removed_on_the_reserve_is_removed(trio):
    t = trio()
    for w in ("base", "cur", "rep"):
        t.sql(w, "INSERT INTO server_state VALUES ('email_password', 'DUMMY')")
    t.sql("rep", "DELETE FROM server_state WHERE key = 'email_password'")
    t.merge()
    assert t.result("SELECT COUNT(*) FROM server_state WHERE key = 'email_password'") == [(0,)]


# ── конфликты уникальных значений ────────────────────────────────────────────

def test_an_address_taken_by_both_sides_stays_with_the_reserves_device_and_the_mains_goes_to_the_report(trio):
    """Устройство, заведённое на основе в последние секунды перед её смертью,
    заняло тот же адрес, что новое на резерве (аллокатор — «первый свободный»)."""
    t = trio()
    cur, rep = t.cur(), t.rep()
    cc = mk_client(cur, "Основа-новый", 2100)
    cd = mk_dev(cur, cc, "Основа-устройство", "10.8.1.9", pub="DUMMY-pub-main")
    rc = mk_client(rep, "Резерв-новый", 2200)
    rd = mk_dev(rep, rc, "Резерв-устройство", "10.8.1.9", pub="DUMMY-pub-reserve")
    rep.update_device_fields(rd, traffic_limit=555)
    rpt = t.merge()
    assert t.result("SELECT name FROM devices WHERE address = '10.8.1.9'") == [("Резерв-устройство",)], \
        "ею пользовались — строка резерва остаётся"
    assert t.result("SELECT COUNT(*) FROM devices WHERE name = 'Основа-устройство'") == [(0,)]
    assert t.result("SELECT COUNT(*) FROM devices WHERE public_key = 'DUMMY-pub-main'") == [(0,)]
    assert t.result("SELECT traffic_limit FROM device_traffic WHERE device_id = (SELECT id FROM devices WHERE address = '10.8.1.9')") == [(555,)], \
        "счётчики устройства резерва — резервные, не осиротевшие основы"
    assert t.result("SELECT COUNT(*) FROM device_traffic") == t.result("SELECT COUNT(*) FROM devices")
    del cd
    assert [(r["table"], r["name"], r["column"], r["value"]) for r in rpt.lost] == [
        ("devices", "Основа-устройство", "address", "10.8.1.9")]


def test_a_telegram_id_taken_by_both_sides_keeps_the_reserves_profile(trio):
    t = trio()
    mk_client(t.cur(), "Основа-Вера", 2300)
    mk_client(t.rep(), "Резерв-Вера", 2300)
    rpt = t.merge()
    assert t.result("SELECT name FROM clients WHERE tg_id = 2300") == [("Резерв-Вера",)]
    assert [r["name"] for r in rpt.lost] == ["Основа-Вера"] and rpt.lost[0]["column"] == "tg_id"


def test_an_address_freed_and_reused_on_the_reserve_does_not_lose_the_new_device(trio):
    """Админ на резерве удалил устройство и завёл новое: аллокатор отдаёт
    освободившийся адрес. Новое устройство обязано дожить до возврата."""
    t = trio()
    rep = t.rep()
    rep.delete_device(t.seeded["da"])
    mk_dev(rep, t.seeded["a"], "Новый телефон", "10.8.1.2", pub="DUMMY-pub-new")
    t.merge()
    assert t.result("SELECT name FROM devices WHERE address = '10.8.1.2'") == [("Новый телефон",)], \
        "устройство, занявшее освободившийся адрес, пропало при слиянии"
    assert t.result("SELECT COUNT(*) FROM device_traffic") == t.result("SELECT COUNT(*) FROM devices"), \
        "счётчики без устройства или устройство без счётчиков"


def test_a_telegram_id_freed_and_reused_on_the_reserve_does_not_lose_the_new_profile(trio):
    t = trio()
    rep = t.rep()
    rep.delete_client(t.seeded["a"])
    mk_client(rep, "Аня снова", 2001)
    t.merge()
    assert t.result("SELECT name FROM clients WHERE tg_id = 2001") == [("Аня снова",)], \
        "профиль, заведённый заново на то же имя в Telegram, пропал при слиянии"
    assert t.result("SELECT COUNT(*) FROM client_subscription WHERE client_id NOT IN (SELECT id FROM clients)") == [(0,)]


def test_a_lazy_row_created_on_both_sides_takes_the_reserves_value(trio):
    """Строки паузы и отсрочки создаются при первой записи: если её сделали и
    основа, и резерв, значение резерва главнее, а не пропадает."""
    t = trio()
    a = t.seeded["a"]
    t.sql("cur", "INSERT INTO client_grace (client_id, grace_used, grace_pending_cut) VALUES (?, 0, 5)", (a,))
    t.sql("rep", "INSERT INTO client_grace (client_id, grace_used, grace_pending_cut) VALUES (?, 1, 9)", (a,))
    t.merge()
    assert t.result("SELECT grace_used FROM client_grace WHERE client_id = ?", (a,)) == [(1,)]


# ── схема: реестр политик и версии ───────────────────────────────────────────

def _live_schema(tmp_path):
    db = Database(str(tmp_path / "schema.db"))
    db.init_schema()
    con = sqlite3.connect(db.path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")} - {"sqlite_sequence"}
        cols = {t: [r[1] for r in con.execute(f"PRAGMA table_info({t})")] for t in tables}
    finally:
        con.close()
        db.close()
    return cols


def test_every_table_of_the_schema_has_a_merge_policy_and_no_policy_is_stale(tmp_path):
    cols = _live_schema(tmp_path)
    assert set(cols) - P.all_tables() == set(), f"новая таблица без правила слияния: {sorted(set(cols) - P.all_tables())}"
    assert P.all_tables() - set(cols) == set(), f"правило для таблицы, которой нет: {sorted(P.all_tables() - set(cols))}"


def test_every_column_of_client_data_has_a_rule_and_no_rule_is_stale(tmp_path):
    cols = _live_schema(tmp_path)
    for table in P.TABLES:
        assert set(cols[table]) == set(P.columns(table)), \
            f"{table}: колонки без правила {sorted(set(cols[table]) - set(P.columns(table)))}, " \
            f"правила без колонки {sorted(set(P.columns(table)) - set(cols[table]))}"


def test_every_rule_is_a_known_kind_and_every_key_column_is_marked_as_key():
    known = {P.CHANGED, P.MASK, P.COUNTER, P.MAX, P.CUR, P.REP, P.KEY}
    for table in P.TABLES:
        rules = P.columns(table)
        assert set(rules.values()) <= known, f"{table}: неизвестное правило"
        for k in P.key_of(table):
            assert rules[k] == P.KEY, f"{table}.{k}: ключ строки должен быть помечен как ключ"


def test_a_new_column_in_the_live_base_stops_the_merge_before_it_touches_anything(trio):
    t = trio()
    t.sql("cur", "ALTER TABLE clients ADD COLUMN novelty TEXT")
    t.sql("rep", "UPDATE clients SET name = 'Изменено на резерве' WHERE id = ?", (t.seeded["a"],))
    with pytest.raises(M.MergeError, match="novelty"):
        t.merge()
    assert t.result("SELECT name FROM clients WHERE id = ?", (t.seeded["a"],)) == [("Аня",)]


def test_a_replica_of_a_newer_version_merges_by_the_common_columns(trio):
    """Резерв новее: в его базе у профилей лишняя колонка — слияние идёт по общим."""
    t = trio()
    t.sql("rep", "ALTER TABLE clients ADD COLUMN novelty TEXT")
    t.sql("rep", "ALTER TABLE devices ADD COLUMN novelty TEXT")
    t.sql("rep", "UPDATE clients SET name = 'Аня (резерв)', novelty = 'x' WHERE id = ?", (t.seeded["a"],))
    t.merge()
    assert t.result("SELECT name FROM clients WHERE id = ?", (t.seeded["a"],)) == [("Аня (резерв)",)]


def test_a_replica_of_an_older_version_merges_by_the_common_columns(trio):
    t = trio()
    t.sql("rep", "ALTER TABLE clients DROP COLUMN tg_username")
    t.sql("rep", "UPDATE clients SET name = 'Аня (резерв)' WHERE id = ?", (t.seeded["a"],))
    t.sql("cur", "UPDATE clients SET tg_username = 'anya' WHERE id = ?", (t.seeded["a"],))
    t.merge()
    assert t.result("SELECT name, tg_username FROM clients WHERE id = ?", (t.seeded["a"],)) == [("Аня (резерв)", "anya")]


# ── общее ────────────────────────────────────────────────────────────────────

def test_merging_a_replica_equal_to_the_snapshot_changes_nothing(trio):
    t = trio()
    t.cur().update_client_fields(t.seeded["a"], name="Основа изменила")
    before = t.result("SELECT * FROM clients ORDER BY id"), t.result("SELECT * FROM device_traffic ORDER BY device_id")
    rpt = t.merge()
    assert (t.result("SELECT * FROM clients ORDER BY id"), t.result("SELECT * FROM device_traffic ORDER BY device_id")) == before
    assert not rpt.inserted and not rpt.deleted and not rpt.lost


def test_the_merge_is_all_or_nothing(trio, monkeypatch):
    """Сбой посреди слияния оставляет базу основы прежней: полуслитая база с
    частью профилей хуже, чем неслитая."""
    t = trio()
    mk_client(t.rep(), "Резерв-новый", 2200)
    t.sql("rep", "UPDATE clients SET name = 'Аня (резерв)' WHERE id = ?", (t.seeded["a"],))
    monkeypatch.setattr(M, "_merge_state", lambda con, rpt: (_ for _ in ()).throw(RuntimeError("сбой")))
    with pytest.raises(RuntimeError):
        t.merge()
    assert t.result("SELECT name FROM clients ORDER BY id") != [], "база читается"
    assert t.result("SELECT COUNT(*) FROM clients WHERE name IN ('Резерв-новый', 'Аня (резерв)')") == [(0,)]


def test_after_the_merge_a_subscription_that_ran_out_meanwhile_is_expired_by_the_recheck(trio):
    """Слияние не пересчитывает сроки: за них отвечает recheck при возврате
    (по фактическому времени, без уведомлений)."""
    from awgbot.domain.backup.merge.recheck import recheck

    def seed(db):
        cid = mk_client(db, "Аня", 2001, end="2020-01-01T00:00:00+03:00")
        mk_dev(db, cid, "Телефон", "10.8.1.2")
        return {"c": cid}
    t = trio(seed)
    t.sql("rep", "UPDATE clients SET name = 'Аня (резерв)'")
    t.merge()
    out = recheck(t.paths["cur"])
    assert out["expired"] == 1
    assert t.result("SELECT s.status, c.name FROM client_subscription s JOIN clients c ON c.id = s.client_id "
                    "WHERE c.id = ?", (t.seeded["c"],)) == [("expired", "Аня (резерв)")]
    assert t.result("SELECT block_reason FROM clients WHERE id = ?", (t.seeded["c"],))[0][0] != 0, "бит блокировки по сроку"


def test_an_archive_month_closed_on_both_sides_is_one_row_with_both_counters(trio):
    """Граница месяца пришлась на away: архив прошлого месяца закрыли обе
    стороны — строка одна, счётчики сложены (не две строки за месяц, не
    удвоенный итог в отчёте)."""
    t = trio()
    did, cid = t.seeded["da"], t.seeded["a"]
    prev = timeutil.shift(timeutil.now().replace(day=1), -1).strftime("%Y-%m")
    for w, (rx, tx) in (("cur", (5, 6)), ("rep", (100, 200))):
        t.sql(w, "INSERT INTO traffic_monthly (device_id, client_id, month, rx, tx, rf_rx, rf_tx, archived_at) "
                 "VALUES (?, ?, ?, ?, ?, 1, 2, '2026-10-01T00:00:00+03:00')", (did, cid, prev, rx, tx))
    t.merge()
    rows = t.result("SELECT rx, tx, rf_rx, rf_tx FROM traffic_monthly WHERE device_id = ? AND month = ?", (did, prev))
    assert rows == [(105, 206, 2, 4)], f"архив месяца после слияния: {rows}"


def test_without_deletions_a_row_removed_on_the_reserve_stays(trio):
    """Снимка нет — база слияния есть копия живой базы: «удалено на резерве»
    вывести нельзя, удаление не выводится вовсе."""
    t = trio()
    t.sql("rep", "DELETE FROM devices WHERE id = ?", (t.seeded["db"],))
    for db in t._open:
        db.close()
    t._open.clear()
    M.merge(t.paths["cur"], t.paths["base"], t.paths["rep"], no_delete=True)
    assert t.result("SELECT COUNT(*) FROM devices WHERE id = ?", (t.seeded["db"],)) == [(1,)], \
        "без снимка устройство удалено слиянием"
