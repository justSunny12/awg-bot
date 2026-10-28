"""Перебазирование хвоста старой эпохи поверх базы держателя (util/rsvjrn/rebase.py,
infra/db/jrn.jrn_rebase) на настоящей схеме: сценарии слияния прежнего пути
(test_reserve_merge.py) в виде «хвост против новой эпохи».

Мир: база на точке расхождения копируется в две — держатель новой эпохи и
бывший держатель; у обоих включён захват. Правки бывшего держателя после
точки — его события (с прежними значениями `o`), это и есть хвост; держатель
меняет свою базу сам. Хвост ложится на базу держателя его соединением.

Правило спора — обратное прежнему слиянию: побеждает новая эпоха (тот, кто
несёт пользователей после передачи), правка хвоста — в отчёт спорного.

Цена ошибки: правка хвоста поверх значения, которое держатель уже сменил, —
у пользователя тихо откатывается продление или имя, сделанное на резерве;
правка, не применённая, хотя держатель колонку не трогал, — теряется продление
с основы за секунды до аварии; вставка поверх занятого id — чужое устройство
получает чужие счётчики; правки не вставшей вставки, легшие на строку
держателя с тем же id, — чужой лимит трафика; удаление изменённой строки —
пропадает устройство, которым пользовались на резерве; спорное без записи в
отчёте — админ не узнаёт, что правка не перенесена; значение ключа или кода в
отчёте — секрет уходит в журнал и в чат.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from awgbot.core import config
from awgbot.infra.db import Database
from awgbot.util.rsvjrn import apply as japply
from awgbot.util.rsvjrn import rebase as jrebase

pytestmark = pytest.mark.integration

_START = "2026-01-01T00:00:00+03:00"
_FUTURE = "2099-01-01T00:00:00+03:00"
BIT_A, BIT_B, BIT_C = 1, 2, 4


def mk_client(db, name, tg, *, end=_FUTURE) -> int:
    cid = db.create_client(name, 3, _START, end, f"INV-DUMMY-{name}")
    db.activate_client(cid, tg)
    return cid


def mk_dev(db, cid, name, addr, pub=None, **kw) -> int:
    return db.create_device(cid, name, pub or f"DUMMY-pub-{name}", "DUMMY-psk", addr, **kw)


def _capture(db) -> None:
    conn = db._connection()
    conn.execute("UPDATE jrn_meta SET capture = 1 WHERE id = 1")
    conn.commit()


def _top(db) -> int:
    return int(db._connection().execute("SELECT COALESCE(MAX(i), 0) FROM jrn_events").fetchone()[0])


class Fork:
    """Держатель новой эпохи (`holder`) и бывший держатель (`tail`) — копии одной
    базы на точке расхождения. `guest_tail` — бывший держатель — бот основы на
    резерве (С9): его захват несёт закрытые ключи и историю."""

    def __init__(self, holder: Database, tmp_path, *, guest_tail: bool = False):
        self.holder = holder
        path = str(tmp_path / "tail.db")
        dst = sqlite3.connect(path)
        try:
            holder._connection().backup(dst)
        finally:
            dst.close()
        old = config.GUEST
        config.GUEST = guest_tail                     # режим триггеров — по открытию соединения
        try:
            self.tail = Database(path)
            self.tail._connection()
        finally:
            config.GUEST = old
        _capture(self.tail)
        _capture(holder)
        self.tail_from = _top(self.tail)
        self.holder_from = _top(holder)

    def events(self) -> list[japply.Event]:
        return japply.events_after(self.tail._connection(), self.tail_from)

    def rebase(self, **kw) -> jrebase.Report:
        return self.holder.jrn_rebase(self.events(), **kw)

    def q(self, sql, args=()):
        return [tuple(r) for r in self.holder._connection().execute(sql, args).fetchall()]

    def holder_events(self) -> list[japply.Event]:
        return japply.events_after(self.holder._connection(), self.holder_from)

    def close(self):
        self.tail.close()


@pytest.fixture()
def fork(db, tmp_path):
    made: list[Fork] = []

    def build(seed=None, *, guest_tail=False):
        seeded = (seed or _basic)(db)
        f = Fork(db, tmp_path, guest_tail=guest_tail)
        f.seeded = seeded
        made.append(f)
        return f
    yield build
    for f in made:
        f.close()


def _basic(db):
    """Точка расхождения: профили «Аня» и «Борис», по одному устройству у каждого."""
    a, b = mk_client(db, "Аня", 2001), mk_client(db, "Борис", 2002)
    da, dbb = mk_dev(db, a, "Телефон", "10.8.1.2"), mk_dev(db, b, "Ноутбук", "10.8.1.3")
    return {"a": a, "b": b, "da": da, "db": dbb}


def _kinds(rpt) -> list[tuple]:
    return [(d["kind"], d["table"], d.get("col")) for d in rpt.disputed]


# ── колонки: применить, уже так, спорное ────────────────────────────────────

def test_a_column_the_holder_did_not_touch_is_applied_and_becomes_a_holder_event(fork):
    """Правка хвоста ложится, а захват держателя делает её событием его эпохи —
    так она вернётся бывшему держателю обычным порядком."""
    f = fork()
    f.tail.update_client_fields(f.seeded["a"], name="Аня К.", period_end="2030-05-05T00:00:00+03:00")
    rpt = f.rebase()
    assert f.q("SELECT name FROM clients WHERE id = ?", (f.seeded["a"],)) == [("Аня К.",)]
    assert f.q("SELECT period_end FROM client_subscription WHERE client_id = ?", (f.seeded["a"],)) == \
        [("2030-05-05T00:00:00+03:00",)], "срок из хвоста не перенесён"
    assert rpt.disputed == [] and rpt.moved == 2, rpt
    mine = [(e.t, e.op, (e.c or {}).get("name")) for e in f.holder_events()]
    assert ("clients", "U", "Аня К.") in mine, f"перенесённое не стало событием держателя: {mine}"


def test_the_same_value_on_both_sides_is_not_a_dispute(fork):
    f = fork()
    f.tail.update_client_fields(f.seeded["a"], name="Аня К.")
    f.holder.update_client_fields(f.seeded["a"], name="Аня К.")
    rpt = f.rebase()
    assert rpt.disputed == [] and rpt.moved == 0, rpt
    assert f.q("SELECT name FROM clients WHERE id = ?", (f.seeded["a"],)) == [("Аня К.",)]


def test_a_column_changed_by_both_keeps_the_holders_value_and_is_disputed_others_apply(fork):
    """Обе эпохи сменили имя по-разному — имя держателя; лимит устройств меняла только
    старая — применён (как «поле, которое вторая сторона не меняла», прежнего слияния)."""
    f = fork()
    f.holder.update_client_fields(f.seeded["a"], name="Аня (резерв)")
    f.tail.update_client_fields(f.seeded["a"], name="Аня (основа)", device_limit=5)
    rpt = f.rebase()
    name, limit = f.q("SELECT name, device_limit FROM clients WHERE id = ?", (f.seeded["a"],))[0]
    assert name == "Аня (резерв)", "новая эпоха проиграла спор по колонке"
    assert limit == 5, "колонку, которую держатель не трогал, хвост не перенёс"
    (d,) = rpt.disputed
    assert (d["kind"], d["table"], d["col"], d["tail"], d["kept"], d["client_id"]) == \
        ("column", "clients", "name", "Аня (основа)", "Аня (резерв)", f.seeded["a"]), d


def test_a_quiet_column_is_never_disputed(fork):
    """Имя в Telegram подтягивает каждая сторона сама — спор по нему админу шум."""
    f = fork()
    f.holder.update_client_fields(f.seeded["a"], tg_name="Anna R")
    f.tail.update_client_fields(f.seeded["a"], tg_name="Anna T")
    f.tail.update_client_fields(f.seeded["b"], tg_name="Boris T")
    rpt = f.rebase()
    assert rpt.disputed == [], rpt.disputed
    assert f.q("SELECT tg_name FROM clients WHERE id = ?", (f.seeded["a"],)) == [("Anna R",)]
    assert f.q("SELECT tg_name FROM clients WHERE id = ?", (f.seeded["b"],)) == [("Boris T",)], \
        "служебная колонка, которую держатель не трогал, не перенесена"


# ── маски — побитно ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("base, holder, tail, want", [
    (BIT_B, BIT_B | BIT_C, BIT_B | BIT_A, BIT_A | BIT_B | BIT_C),   # хвост поставил A, держатель — C: оба
    (BIT_B, BIT_B | BIT_C, 0, BIT_C),                                  # хвост снял B: C держателя остался
    (0, BIT_C, 0, BIT_C),                                              # хвост не менял: как у держателя
    (BIT_A, 0, BIT_A, 0),                                              # хвост не менял, держатель снял
    (BIT_A | BIT_B, BIT_A | BIT_B, BIT_A, BIT_A),                      # хвост снял только B
    (0, BIT_A, BIT_A | BIT_B, BIT_A | BIT_B),                          # оба поставили A, хвост ещё B
])
def test_block_masks_rebase_bit_by_bit_without_disputes(fork, base, holder, tail, want):
    def seed(db):
        cid = mk_client(db, "Аня", 2001)
        did = mk_dev(db, cid, "Телефон", "10.8.1.2")
        conn = db._connection()
        conn.execute("UPDATE clients SET block_reason = ? WHERE id = ?", (base, cid))
        conn.execute("UPDATE devices SET block_reason = ? WHERE id = ?", (base, did))
        conn.commit()
        return {"c": cid, "d": did}
    f = fork(seed)
    for side, mask in ((f.holder, holder), (f.tail, tail)):
        conn = side._connection()
        conn.execute("UPDATE clients SET block_reason = ? WHERE id = ?", (mask, f.seeded["c"]))
        conn.execute("UPDATE devices SET block_reason = ? WHERE id = ?", (mask, f.seeded["d"]))
        conn.commit()
    rpt = f.rebase()
    assert f.q("SELECT block_reason FROM clients WHERE id = ?", (f.seeded["c"],)) == [(want,)], "маска профиля"
    assert f.q("SELECT block_reason FROM devices WHERE id = ?", (f.seeded["d"],)) == [(want,)], "маска устройства"
    assert rpt.disputed == [], f"маска — побитно, спора по биту не бывает: {rpt.disputed}"


# ── вставки и перенос id ────────────────────────────────────────────────────

def test_a_profile_and_a_device_created_in_the_tail_arrive_with_their_own_rows(fork):
    f = fork()
    cid = mk_client(f.tail, "Вера", 2003)
    did = mk_dev(f.tail, cid, "Планшет", "10.8.1.4")
    f.tail.add_routing_domain(cid, "example.org")
    rpt = f.rebase()
    assert f.q("SELECT c.id, c.tg_id, d.id, d.address FROM clients c JOIN devices d ON d.client_id = c.id "
               "WHERE c.name = 'Вера'") == [(cid, 2003, did, "10.8.1.4")], "id свободны — переносятся как есть"
    for table, col, key in (("device_traffic", "device_id", did), ("client_subscription", "client_id", cid),
                            ("client_quota", "client_id", cid)):
        assert f.q(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (key,)) == [(1,)], f"нет строки {table}"
    assert f.q("SELECT domain FROM client_routing_domains WHERE client_id = ?", (cid,)) == [("example.org",)]
    assert rpt.disputed == [], rpt.disputed


def test_rows_created_on_both_sides_with_the_same_id_move_the_tails_one_with_all_references(fork):
    """Держатель успел завести профиль с тем же id: оба живут, ссылки хвоста —
    на новый id (устройство, счётчики, подписка, домены, правка лимита)."""
    f = fork()
    hc = mk_client(f.holder, "Резерв-новый", 2100)
    hd = mk_dev(f.holder, hc, "Резерв-устройство", "10.8.1.10")
    tc = mk_client(f.tail, "Основа-новый", 2200)
    td = mk_dev(f.tail, tc, "Основа-устройство", "10.8.1.20")
    assert (hc, hd) == (tc, td), "условие теста: оба новых получили одинаковые id"
    f.tail.add_routing_domain(tc, "example.org")
    f.tail.update_device_fields(td, traffic_limit=777)
    rpt = f.rebase()
    names = dict(f.q("SELECT name, id FROM clients"))
    assert names["Резерв-новый"] == hc, "строка держателя переехала"
    new_c = names["Основа-новый"]
    assert new_c != hc
    devs = dict(f.q("SELECT name, id FROM devices"))
    new_d = devs["Основа-устройство"]
    assert devs["Резерв-устройство"] == hd and new_d != hd
    assert f.q("SELECT client_id FROM devices WHERE id = ?", (new_d,)) == [(new_c,)], "устройство хвоста — у чужого профиля"
    assert f.q("SELECT traffic_limit FROM device_traffic WHERE device_id = ?", (new_d,)) == [(777,)]
    assert f.q("SELECT traffic_limit FROM device_traffic WHERE device_id = ?", (hd,)) == [(0,)], \
        "правка лимита хвоста легла на устройство держателя с тем же id"
    assert f.q("SELECT domain FROM client_routing_domains WHERE client_id = ?", (new_c,)) == [("example.org",)]
    assert f.q("SELECT COUNT(*) FROM client_routing_domains WHERE client_id = ?", (hc,)) == [(0,)]
    assert f.q("SELECT COUNT(*) FROM client_subscription WHERE client_id = ?", (new_c,)) == [(1,)]
    assert rpt.disputed == [], rpt.disputed


def test_the_history_of_a_moved_profile_points_to_its_new_id(fork):
    """С9: история с бота на резерве едет дописками — ссылка на профиль, получивший
    новый id, переписана, а не указывает на чужой профиль держателя."""
    f = fork(guest_tail=True)
    mk_client(f.holder, "Здешний-новый", 2100)
    tc = mk_client(f.tail, "Резерв-новый", 2200)
    conn = f.tail._connection()
    conn.execute("INSERT INTO client_block_histories (client_id, block_reason, archived_at, close_reason) "
                 "VALUES (?, 2, '2026-10-02', 'tail')", (tc,))
    conn.commit()
    assert [e.t for e in f.events()].count("client_block_histories") == 1, "условие теста: история в хвосте"
    f.rebase()
    new_id = f.q("SELECT id FROM clients WHERE name = 'Резерв-новый'")[0][0]
    assert new_id != tc
    assert f.q("SELECT client_id FROM client_block_histories WHERE close_reason = 'tail'") == [(new_id,)], \
        "история хвоста указывает на профиль держателя"


def test_a_row_of_a_table_without_a_remap_that_exists_at_the_holder_is_taken_as_an_edit(fork):
    """Ленивая строка (отсрочка) заведена обеими эпохами: значение держателя, правка
    хвоста — спорное; служебное поле отсрочки — без спора."""
    f = fork()
    a = f.seeded["a"]
    f.holder._connection().execute(
        "INSERT INTO client_grace (client_id, grace_used, grace_pending_cut) VALUES (?, 0, 5)", (a,))
    f.holder._connection().commit()
    f.tail._connection().execute(
        "INSERT INTO client_grace (client_id, grace_used, grace_pending_cut) VALUES (?, 1, 9)", (a,))
    f.tail._connection().commit()
    rpt = f.rebase()
    assert f.q("SELECT grace_used FROM client_grace WHERE client_id = ?", (a,)) == [(0,)], "новая эпоха проиграла"
    assert _kinds(rpt) == [("column", "client_grace", "grace_used")], rpt.disputed


# ── уникальность ────────────────────────────────────────────────────────────

def test_an_address_taken_by_the_holder_is_a_dispute_and_the_tails_edits_do_not_land_on_its_row(fork):
    """Держатель завёл устройство на тот же адрес (и тот же id). Вставка хвоста не
    встаёт — спорное «кем занято»; правки этого устройства (счётчики, лимит) не
    ложатся на устройство держателя с тем же id."""
    f = fork()
    hc = mk_client(f.holder, "Анна", 2100)
    hd = mk_dev(f.holder, hc, "Pixel", "10.8.1.9", pub="DUMMY-pub-holder")
    tc = mk_client(f.tail, "Олег", 2200)
    td = mk_dev(f.tail, tc, "iPad", "10.8.1.9", pub="DUMMY-pub-tail")
    assert hd == td, "условие теста: тот же id"
    f.tail.update_device_fields(td, traffic_limit=555, name="iPad Олега")
    rpt = f.rebase()
    assert f.q("SELECT name FROM devices WHERE address = '10.8.1.9'") == [("Pixel",)], "адрес у держателя отняли"
    assert f.q("SELECT COUNT(*) FROM devices WHERE public_key = 'DUMMY-pub-tail'") == [(0,)]
    assert f.q("SELECT traffic_limit FROM device_traffic WHERE device_id = ?", (hd,)) == [(0,)], \
        "лимит не вставшего устройства хвоста лёг на устройство держателя"
    assert f.q("SELECT COUNT(*) FROM device_traffic") == f.q("SELECT COUNT(*) FROM devices"), \
        "счётчики без устройства или устройство без счётчиков"
    unique = [d for d in rpt.disputed if d["kind"] == "unique"]
    assert len(unique) == 1, rpt.disputed
    u = unique[0]
    assert (u["table"], u["col"], u["taken_by"]["name"], u["taken_by"]["client_id"]) == \
        ("devices", "address", "Pixel", hc), u
    assert u["tail"].get("name") == "iPad" and u["tail"].get("address") == "10.8.1.9", u
    orphans = [d for d in rpt.disputed if d["kind"] == "gone"]
    assert orphans and all(d.get("orphan") for d in orphans), f"правки не вставшего устройства не в отчёте: {rpt.disputed}"
    new_oleg = f.q("SELECT id FROM clients WHERE name = 'Олег'")
    assert new_oleg, "профиль хвоста не вставлен — спор только по устройству"


def test_a_telegram_id_taken_by_the_holder_keeps_the_holders_profile(fork):
    """Telegram профиля проставляет активация — правкой (вставка идёт с пустым): спорное
    по уникальности обязано назвать, кем занято, — иначе строка Т-Ж4 без занявшего."""
    f = fork()
    mk_client(f.holder, "Вера на резерве", 2300)
    mk_client(f.tail, "Вера здесь", 2300)
    rpt = f.rebase()
    assert f.q("SELECT name FROM clients WHERE tg_id = 2300") == [("Вера на резерве",)]
    u = [d for d in rpt.disputed if d["kind"] == "unique"]
    assert len(u) == 1, rpt.disputed
    assert u[0]["taken_by"] and u[0]["taken_by"].get("name") == "Вера на резерве" and u[0]["col"] == "tg_id", \
        f"спорное по Telegram без «кем занято»: {u[0]}"
    assert f.q("SELECT COUNT(*) FROM client_subscription WHERE client_id NOT IN (SELECT id FROM clients)") == [(0,)]
    assert f.q("SELECT COUNT(*) FROM client_quota WHERE client_id NOT IN (SELECT id FROM clients)") == [(0,)]


def test_an_address_freed_and_reused_in_the_tail_does_not_lose_the_new_device(fork):
    f = fork()
    f.tail.delete_device(f.seeded["da"])
    mk_dev(f.tail, f.seeded["a"], "Новый телефон", "10.8.1.2", pub="DUMMY-pub-new")
    rpt = f.rebase()
    assert f.q("SELECT name FROM devices WHERE address = '10.8.1.2'") == [("Новый телефон",)], \
        "устройство, занявшее освободившийся адрес, пропало"
    assert f.q("SELECT COUNT(*) FROM device_traffic") == f.q("SELECT COUNT(*) FROM devices")
    assert rpt.disputed == [], rpt.disputed


def test_a_telegram_id_freed_and_reused_in_the_tail_does_not_lose_the_new_profile(fork):
    f = fork()
    f.tail.delete_client(f.seeded["a"])
    mk_client(f.tail, "Аня снова", 2001)
    f.rebase()
    assert f.q("SELECT name FROM clients WHERE tg_id = 2001") == [("Аня снова",)]
    assert f.q("SELECT COUNT(*) FROM client_subscription WHERE client_id NOT IN (SELECT id FROM clients)") == [(0,)]


def test_an_integrity_error_other_than_unique_is_failed_and_its_row_is_an_orphan(fork):
    """Вставка без обязательного поля (событие выпуска с другой схемой) — «не встало»,
    а не «занято»; дальнейшие правки той же строки не ложатся на строку держателя."""
    f = fork()
    mk_dev(f.holder, f.seeded["a"], "Планшет", "10.8.1.30")
    hd = f.q("SELECT id FROM devices WHERE name = 'Планшет'")[0][0]
    evs = [japply.Event(i=101, e=1, t="devices", op="I", k={"id": hd}, c={"id": hd, "name": "Без ключа"}),
           japply.Event(i=102, e=1, t="device_traffic", op="U", k={"device_id": hd}, c={"traffic_limit": 9},
                        o={"traffic_limit": 0})]
    rpt = f.holder.jrn_rebase(evs)
    assert [(d["kind"], d["table"]) for d in rpt.disputed] == [("failed", "devices"), ("gone", "device_traffic")], \
        rpt.disputed
    assert f.q("SELECT traffic_limit FROM device_traffic WHERE device_id = ?", (hd,)) == [(0,)]


# ── удаления ────────────────────────────────────────────────────────────────

def test_a_device_removed_in_the_tail_is_removed_with_its_rows(fork):
    f = fork()
    f.tail.delete_device(f.seeded["da"])
    rpt = f.rebase()
    assert f.q("SELECT COUNT(*) FROM devices WHERE id = ?", (f.seeded["da"],)) == [(0,)]
    assert f.q("SELECT COUNT(*) FROM device_traffic WHERE device_id = ?", (f.seeded["da"],)) == [(0,)]
    assert f.q("SELECT COUNT(*) FROM devices WHERE id = ?", (f.seeded["db"],)) == [(1,)], "соседнее устройство тронуто"
    assert rpt.disputed == []


def test_a_profile_removed_in_the_tail_takes_its_devices_and_rows_with_it(fork):
    f = fork()
    f.tail.delete_client(f.seeded["a"])
    f.rebase()
    a = f.seeded["a"]
    for sql in ("SELECT COUNT(*) FROM clients WHERE id = ?", "SELECT COUNT(*) FROM devices WHERE client_id = ?",
                "SELECT COUNT(*) FROM client_subscription WHERE client_id = ?"):
        assert f.q(sql, (a,)) == [(0,)], sql
    assert f.q("SELECT COUNT(*) FROM clients WHERE id = ?", (f.seeded["b"],)) == [(1,)]


def test_a_removal_of_a_row_the_holder_changed_is_a_dispute_and_the_row_stays(fork):
    """Держатель переименовал устройство, хвост его удалил: устройством пользуются на
    резерве — остаётся, удаление — спорное."""
    f = fork()
    f.holder.update_device_fields(f.seeded["da"], name="Телефон Ани")
    f.tail.delete_device(f.seeded["da"])
    rpt = f.rebase()
    assert f.q("SELECT name FROM devices WHERE id = ?", (f.seeded["da"],)) == [("Телефон Ани",)]
    assert ("delete", "devices", "name") in _kinds(rpt), rpt.disputed


def test_an_edit_of_a_row_the_holder_removed_is_gone_and_not_brought_back(fork):
    f = fork()
    f.holder.delete_device(f.seeded["da"])
    f.tail.update_device_fields(f.seeded["da"], name="Переименован")
    rpt = f.rebase()
    assert f.q("SELECT COUNT(*) FROM devices WHERE id = ?", (f.seeded["da"],)) == [(0,)], "удалённое вернулось"
    assert ("gone", "devices", "") in _kinds(rpt), rpt.disputed


# ── счётчики, данные хоста ──────────────────────────────────────────────────

def test_month_and_period_resets_of_the_tail_are_skipped(fork):
    """Сбросы по времени держатель делает сам — повтор из хвоста обнулил бы его расход."""
    f = fork()
    conn = f.holder._connection()
    conn.execute("UPDATE device_traffic SET traffic_rx_month = 500, traffic_rx_period = 700")
    conn.commit()
    evs = [japply.Event(i=201, e=1, t="device_traffic", op="month", k={"month": "2026-11"}),
           japply.Event(i=202, e=1, t="device_traffic", op="period", k={"client_id": f.seeded["a"]})]
    rpt = f.holder.jrn_rebase(evs)
    assert f.q("SELECT DISTINCT traffic_rx_month, traffic_rx_period FROM device_traffic") == [(500, 700)]
    assert rpt.skipped == 2 and rpt.moved == 0 and rpt.disputed == [], rpt


def test_a_traffic_merge_adds_up_when_both_rows_are_there_and_is_skipped_otherwise(fork):
    f = fork()
    conn = f.holder._connection()
    conn.execute("UPDATE device_traffic SET traffic_rx_month = 100 WHERE device_id = ?", (f.seeded["da"],))
    conn.execute("UPDATE device_traffic SET traffic_rx_month = 40 WHERE device_id = ?", (f.seeded["db"],))
    conn.commit()
    ok = f.holder.jrn_rebase([japply.Event(i=301, e=1, t="device_traffic", op="tmerge",
                                           k={"src": f.seeded["db"], "dst": f.seeded["da"]})])
    assert f.q("SELECT traffic_rx_month FROM device_traffic WHERE device_id = ?", (f.seeded["da"],)) == [(140,)]
    assert ok.moved == 1
    miss = f.holder.jrn_rebase([japply.Event(i=302, e=1, t="device_traffic", op="tmerge",
                                             k={"src": 9999, "dst": f.seeded["da"]})])
    assert miss.skipped == 1 and miss.moved == 0
    assert f.q("SELECT traffic_rx_month FROM device_traffic WHERE device_id = ?", (f.seeded["da"],)) == [(140,)]


def test_host_rows_of_the_tail_are_skipped(fork):
    """Слоты шлюзов — данные хоста основы: держатель их не захватывает, из хвоста не
    ставит (не вернулись бы)."""
    f = fork()
    f.tail.gateway_add(f.seeded["da"], "awglink", 443, "10.99.99.0/30")
    evs = f.events()
    assert any(e.t == "gateways" for e in evs), "условие теста: слот в хвосте"
    rpt = f.holder.jrn_rebase(evs)
    assert f.q("SELECT COUNT(*) FROM gateways") == [(0,)], "слот шлюза из хвоста встал у держателя"
    assert rpt.skipped >= 1


def test_the_whole_rebase_is_one_transaction_with_the_mark(fork):
    """Отметка «перенесён» пишется той же транзакцией: её отказ откатывает и перенесённое
    — иначе повтор хвоста перенёс бы его второй раз."""
    f = fork()
    f.tail.update_client_fields(f.seeded["a"], name="Аня К.")

    def boom(conn, rpt):
        raise RuntimeError("отметка не записалась")
    with pytest.raises(RuntimeError):
        f.rebase(before_commit=boom)
    assert f.q("SELECT name FROM clients WHERE id = ?", (f.seeded["a"],)) == [("Аня",)], "перенесённое без отметки"
    assert f.holder_events() == [], "события держателя без перенесённого"


# ── секреты ─────────────────────────────────────────────────────────────────

def test_an_empty_secret_in_the_tail_does_not_erase_the_holders_key(fork):
    """С9: у бота на резерве закрытого ключа основы нет — его правка устройства несёт
    пустой ключ; у основы ключ остаётся, правка ложится, спора нет."""
    def seed(db):
        s = _basic(db)
        db.update_device_fields(s["da"], private_key="DUMMY-priv-main")
        return s
    f = fork(seed, guest_tail=True)
    f.tail._connection().execute("UPDATE devices SET private_key = NULL WHERE id = ?", (f.seeded["da"],))
    f.tail._connection().commit()
    f.tail_from = _top(f.tail)                        # так было у бота на резерве с начала
    f.tail.update_device_fields(f.seeded["da"], name="Телефон Ани")
    assert any("private_key" in (e.c or {}) for e in f.events()), "условие теста: захват гостя несёт ключ"
    rpt = f.rebase()
    assert f.q("SELECT name, private_key FROM devices WHERE id = ?", (f.seeded["da"],)) == \
        [("Телефон Ани", "DUMMY-priv-main")], "пустой ключ из хвоста стёр закрытый ключ основы"
    assert rpt.disputed == [], rpt.disputed


def test_a_removal_from_the_guest_tail_is_not_a_dispute_over_a_secret(fork):
    def seed(db):
        s = _basic(db)
        db.update_device_fields(s["da"], private_key="DUMMY-priv-main")
        return s
    f = fork(seed, guest_tail=True)
    f.tail._connection().execute("UPDATE devices SET private_key = NULL WHERE id = ?", (f.seeded["da"],))
    f.tail._connection().commit()
    f.tail_from = _top(f.tail)
    f.tail.delete_device(f.seeded["da"])
    rpt = f.rebase()
    assert f.q("SELECT COUNT(*) FROM devices WHERE id = ?", (f.seeded["da"],)) == [(0,)], \
        "удаление не перенесено из-за ключа, которого у бота на резерве не было"
    assert rpt.disputed == [], rpt.disputed


def test_a_new_device_of_the_guest_tail_keeps_its_own_key(fork):
    f = fork(guest_tail=True)
    did = mk_dev(f.tail, f.seeded["a"], "Планшет", "10.8.1.40", private_key="DUMMY-priv-guest")
    f.rebase()
    assert f.q("SELECT private_key FROM devices WHERE id = ?", (did,)) == [("DUMMY-priv-guest",)]


def test_a_disputed_key_or_code_never_carries_its_values(fork):
    """PSK и код приглашения сменили обе стороны: спорное — без значений (ни в отчёт,
    ни в журнал, ни в Т-Ж4)."""
    f = fork()
    conn_h, conn_t = f.holder._connection(), f.tail._connection()
    conn_h.execute("UPDATE devices SET preshared_key = 'DUMMY-psk-holder' WHERE id = ?", (f.seeded["da"],))
    conn_h.execute("UPDATE clients SET invite_code = 'DUMMY-inv-holder' WHERE id = ?", (f.seeded["b"],))
    conn_h.commit()
    conn_t.execute("UPDATE devices SET preshared_key = 'DUMMY-psk-tail' WHERE id = ?", (f.seeded["da"],))
    conn_t.execute("UPDATE clients SET invite_code = 'DUMMY-inv-tail' WHERE id = ?", (f.seeded["b"],))
    conn_t.commit()
    rpt = f.rebase()
    cols = sorted(d["col"] for d in rpt.disputed)
    assert cols == ["invite_code", "preshared_key"], rpt.disputed
    assert all(d.get("secret") and d["tail"] is None and d["kept"] is None for d in rpt.disputed), rpt.disputed
    dump = json.dumps(rpt.disputed, ensure_ascii=False)
    for v in ("DUMMY-psk-holder", "DUMMY-psk-tail", "DUMMY-inv-holder", "DUMMY-inv-tail"):
        assert v not in dump, f"значение секрета в отчёте: {v}"
    assert f.q("SELECT preshared_key FROM devices WHERE id = ?", (f.seeded["da"],)) == [("DUMMY-psk-holder",)]


# ── отчёт целиком ───────────────────────────────────────────────────────────

def _twelve(db):
    return {"ids": [mk_client(db, f"П{n:02d}", 3000 + n) for n in range(12)]}


def test_twelve_disputes_are_all_in_the_report(fork):
    """Список спорного полный, без обрезки: снимок и строки делает основа."""
    f = fork(_twelve)
    for cid in f.seeded["ids"]:
        f.holder.update_client_fields(cid, name=f"Резерв {cid}")
        f.tail.update_client_fields(cid, name=f"Здесь {cid}")
    rpt = f.rebase()
    assert len(rpt.disputed) == 12, len(rpt.disputed)
    assert sorted(d["client_id"] for d in rpt.disputed) == sorted(f.seeded["ids"])


def test_the_holders_connection_keeps_working_after_a_rebase(fork):
    """Перебазирование идёт соединением держателя — тем же, через которое дальше читает
    весь процесс: после него строки базы — по-прежнему по именам колонок (иначе первое
    же чтение настроек или профиля падает, и ответ на хвост не уходит)."""
    f = fork()
    f.tail.update_device_fields(f.seeded["da"], name="Телефон Ани")
    f.rebase()
    assert f.holder.get_client(f.seeded["a"]).name == "Аня", "соединение держателя сломано перебазированием"
    assert f.holder.get_state("db_line") is not None
