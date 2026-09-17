"""Integration: awgbot.infra.db на настоящей временной SQLite (без awg)."""
import pytest


pytestmark = pytest.mark.integration


# ── клиенты ──────────────────────────────────────────────────────────────────
def test_create_and_fetch_client(db):
    cid = db.create_client("Вася", 3, "2026-01-01T00:00:00+03:00",
                           "2027-01-01T00:00:00+03:00", "CabcABC12345",
                           traffic_limit=1000, period_kind="year")
    c = db.get_client(cid)
    assert c is not None
    assert c.name == "Вася" and c.device_limit == 3
    assert c.activation_status == "pending" and c.tg_id is None
    assert c.period_end == "2027-01-01T00:00:00+03:00"
    assert c.period_kind == "year" and c.traffic_limit == 1000
    assert db.get_client_by_invite("CabcABC12345").id == cid


def test_activate_client_sets_tg_and_active(db):
    cid = db.create_client("Петя", 1, "2026-01-01T00:00:00+03:00",
                           "2027-01-01T00:00:00+03:00", "Cxyz00000000")
    db.activate_client(cid, tg_id=555)
    c = db.get_client(cid)
    assert c.tg_id == 555 and c.activation_status == "active"
    assert db.get_client_by_tg(555).id == cid


def test_list_clients_excludes_service_by_default(db):
    db.create_client("A", 1, "s", "e", "Ca0000000000")
    non_service = db.list_clients(include_service=False)
    assert all(c.is_service == 0 for c in non_service)
    assert any(c.name == "A" for c in non_service)


def test_update_client_fields(db):
    cid = db.create_client("A", 1, "s", "e", "Cb0000000000")
    db.update_client_fields(cid, block_reason=5, device_limit=9)
    c = db.get_client(cid)
    assert c.block_reason == 5 and c.device_limit == 9


# ── устройства ───────────────────────────────────────────────────────────────
def test_create_device_and_counts(db):
    cid = db.create_client("A", 3, "s", "e", "Cc0000000000")
    did = db.create_device(cid, "Phone", "PUB1", "PSK", "10.8.1.5", private_key="PRIV1", traffic_limit=500)
    d = db.get_device(did)
    assert d.name == "Phone" and d.address == "10.8.1.5"
    assert d.public_key == "PUB1" and d.private_key == "PRIV1"
    assert d.is_managed and d.traffic_limit == 500
    assert db.count_devices(cid) == 1
    assert [x.id for x in db.list_devices(cid)] == [did]
    assert next(d for d in db.list_all_devices() if d.public_key == "PUB1").id == did


def test_delete_device(db):
    cid = db.create_client("A", 3, "s", "e", "Cd0000000000")
    did = db.create_device(cid, "P", "PUB", "PSK", "10.8.1.6")
    db.delete_device(did)
    assert db.get_device(did) is None
    assert db.count_devices(cid) == 0


# ── аллокация IP (общий пул с приложением) ───────────────────────────────────
def test_allocate_ip_first_free(db):
    assert db.allocate_ip(subnet_prefix="10.8.1", start_host=1, end_host=254) == "10.8.1.1"


def test_allocate_ip_skips_db_and_live(db):
    cid = db.create_client("A", 5, "s", "e", "Ce0000000000")
    db.create_device(cid, "P1", "PUBa", "PSK", "10.8.1.1")
    # .2 занят «живым конфигом» (пир, которого нет в БД)
    ip = db.allocate_ip(subnet_prefix="10.8.1", occupied_extra={"10.8.1.2"},
                        start_host=1, end_host=254)
    assert ip == "10.8.1.3"


def test_allocate_ip_pool_exhausted(db):
    with pytest.raises(RuntimeError):
        db.allocate_ip(subnet_prefix="10.8.1", occupied_extra={"10.8.1.1"},
                       start_host=1, end_host=1)


# ── трафик и метки уведомлений ───────────────────────────────────────────────
def test_add_traffic_accumulates(db):
    cid = db.create_client("A", 3, "s", "e", "Cf0000000000")
    did = db.create_device(cid, "P", "PUB", "PSK", "10.8.1.7")
    db.add_traffic_bulk([(did, 100, 200)])
    db.add_traffic_bulk([(did, 50, 25)])
    d = db.get_device(did)
    assert d.traffic_rx_month == 150 and d.traffic_tx_month == 225
    tot = db.get_client_traffic(cid)
    assert tot["rx_month"] == 150 and tot["tx_month"] == 225


def test_notified_thresholds_set(db):
    cid = db.create_client("A", 3, "s", "e", "Cg0000000000")
    assert db.get_notified(cid) == set()
    db.add_notified(cid, 1440)
    db.add_notified(cid, 120)
    assert db.get_notified(cid) == {1440, 120}
    db.reset_notified(cid)
    assert db.get_notified(cid) == set()


def test_traffic_notified_markers(db):
    cid = db.create_client("A", 3, "s", "e", "Ch0000000000")
    db.add_traffic_notified(cid, "cli80")
    assert "cli80" in db.get_traffic_notified(cid)
    db.reset_traffic_notified(cid)
    assert db.get_traffic_notified(cid) == set()


# ── key-value state (служебное) ──────────────────────────────────────────────
def test_state_kv(db):
    assert db.get_state("missing") is None
    db.set_state("k", "v")
    assert db.get_state("k") == "v"
    db.set_state("k", "v2")
    assert db.get_state("k") == "v2"


# ── схема поднимает служебного клиента ───────────────────────────────────────
def test_service_client_exists(db):
    sid = db.get_service_client_id()
    assert sid > 0
    assert db.get_client(sid).is_service == 1


def test_content_msg_ids_track_and_pop(db):
    """Трекинг id контент-сообщений: добавили → забрали → очистилось."""
    db.add_content_msg_id(123, 10)
    db.add_content_msg_id(123, 11)
    ids = db.pop_content_msg_ids(123)
    assert ids == [10, 11]
    assert db.pop_content_msg_ids(123) == []          # очистилось


def test_content_cleanup_removes_invite_style_msgs(db):
    """Инвайт-сообщения трекаются как контент → pop отдаёт их для удаления."""
    db.add_content_msg_id(555, 100)   # напр. head профиля
    db.add_content_msg_id(555, 101)   # сообщение со ссылкой-инвайтом
    assert db.pop_content_msg_ids(555) == [100, 101]


def test_list_clients_admin_first(db):
    """Профиль администратора всегда первым в списке, остальные — по алфавиту."""
    db.create_client("Борис", 1, "2025-01-01", "2025-12-01", "c1")
    db.create_client("Админ", 1, "2025-01-01", "2025-12-01", "c2")
    db.create_client("Анна", 1, "2025-01-01", "2025-12-01", "c3")
    db.activate_client("c1", 10)
    db.activate_client("c2", 42)
    db.activate_client("c3", 11)
    names = [c.name for c in db.list_clients(admin_first_tg=42)]
    assert names[0] == "Админ"
    assert names[1:] == ["Анна", "Борис"]


def test_fresh_schema_is_complete_and_idempotent(tmp_path):
    """Минимальная поддерживаемая версия — v2.10.0, её схема полная: SCHEMA
    создаёт всё, что читает код, без доводящих миграций; повторный init_schema
    ничего не ломает. Единственность шлюза держит индекс в схеме, а не код.
    Прыжок с реальных данных v2.10.0 — tests/integration/test_update_jump.py."""
    import sqlite3
    from awgbot.infra.db import Database
    db = Database(str(tmp_path / "fresh.db"))
    db.init_schema(); db.init_schema()
    con = db._connection()
    cols = lambda t: {r["name"] for r in con.execute(f"PRAGMA table_info({t})")}
    assert {"content_msg_ids"} <= cols("ui_state")
    assert {"resume_code"} <= cols("client_pause")
    assert {"routing_allowed"} <= cols("clients") and "routing_master" not in cols("clients")
    assert {"routing_on", "iface", "twin_of", "is_gateway"} <= cols("devices")
    assert "full_access_link" not in cols("devices")
    assert {"last_update"} <= cols("traffic_samples") and "sampled_at" not in cols("traffic_samples")
    assert "mode" not in cols("client_routing_domains")

    cid = db.create_client(name="c", device_limit=3, period_start="2026-01-01",
                           period_end="2027-01-01", invite_code="A")
    a = db.create_device(cid, "a", "PA", "S", "10.8.1.2", private_key="k")
    b = db.create_device(cid, "b", "PB", "S", "10.8.1.3", private_key="k")
    # предпочтительный слот — не более одного, держит частичный уникальный индекс
    db.gateway_add(a, "awglink", 443, "10.99.99.0/30")
    db.gateway_add(b, "awglink2", 8443, "10.99.99.4/30")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE gateways SET preferred = 1 WHERE device_id = ?", (b,))
    db.close()


def test_content_cleanup_dedup(db):
    """add_content_msg_id дедуплицирует — один message_id не задваивается."""
    db.add_content_msg_id(200, 50)
    db.add_content_msg_id(200, 50)   # тот же id (edit того же nav)
    db.add_content_msg_id(200, 51)
    assert db.pop_content_msg_ids(200) == [50, 51]


