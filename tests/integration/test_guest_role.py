"""
Роль «гость» и переданные устройства (docs/guest-role.md), слой модели:
гостевой профиль, держатель устройства, правило одного дарителя, закрытие
пустого гостя, переход гость → владелец, перенос прежних «друзей».
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.core.blocks import DeviceBlock
from awgbot.infra.db import Database

pytestmark = pytest.mark.integration


def _lend(services, owner, name, tg, tg_name=""):
    dc = services.add_device(owner.id, name)
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=tg,
                                   tg_name=tg_name)
    assert res.ok, res.reason
    return dc, res


# ── гость и держатель ────────────────────────────────────────────────────────

def test_first_code_creates_a_guest_profile_holding_the_device(services, make_active_client):
    owner = make_active_client(tg_id=900, device_limit=3)
    dc, res = _lend(services, owner, "Телефон", 9900, "Артём")
    guest = res.holder
    assert guest.is_guest and guest.tg_id == 9900 and guest.name == "Артём"
    assert guest.device_limit == 0 and guest.activation_status == "active"
    dev = services.db.get_device(dc.device_id)
    assert dev.client_id == owner.id and dev.holder_client_id == guest.id
    assert dev.holder_tg_id == 9900 and dev.holder_name == "Артём"
    assert dev.owner_tg_id == 900 and dev.owner_name == owner.name
    assert dev.is_lent and dev.friend_status == "active" and dev.friend_tg_id == 9900
    assert dev.friend is None, "приглашение исполнено — строки нет"
    # слот и квота — у владельца
    assert services.db.count_devices(owner.id) == 1
    assert services.db.count_devices(guest.id) == 0
    assert services.guest_donor(guest).id == owner.id
    # гости не попадают в списки владельцев
    assert guest.id not in [c.id for c in services.db.list_clients()]
    assert guest.id in [c.id for c in services.db.list_clients(include_guests=True)]


def test_pending_code_is_still_a_friend_row(services, make_active_client):
    owner = make_active_client(tg_id=901)
    dc = services.add_device(owner.id, "Телефон")
    code = services.make_device_friendly(dc.device_id)
    dev = services.db.get_device(dc.device_id)
    assert dev.friend_status == "pending" and dev.friend_code == code and not dev.is_lent


def test_guest_closes_when_last_held_device_is_removed(services, make_active_client):
    owner = make_active_client(tg_id=902, device_limit=3)
    a, res = _lend(services, owner, "A", 9902)
    b, _ = _lend(services, owner, "B", 9902)
    guest_id = res.holder.id
    assert services.remove_device(a.device_id) == 9902, "держатель — адресат «удалено владельцем»"
    assert services.db.get_client(guest_id) is not None
    services.remove_device(b.device_id)
    assert services.db.get_client(guest_id) is None, "гость без устройств должен закрыться"
    assert services.db.get_client_by_tg(9902) is None


def test_owner_client_holding_a_device_is_not_closed(services, make_active_client):
    owner = make_active_client(tg_id=903)
    holder = make_active_client(tg_id=904)
    dc, _ = _lend(services, owner, "A", 904)
    services.remove_device(dc.device_id)
    assert services.db.get_client(holder.id) is not None


def test_admin_reassign_to_the_holder_clears_the_holder(services, make_active_client):
    owner = make_active_client(tg_id=905, device_limit=2)
    holder = make_active_client(tg_id=906, device_limit=2)
    dc, _ = _lend(services, owner, "A", 906)
    services.reassign_device(dc.device_id, holder.id)
    dev = services.db.get_device(dc.device_id)
    assert dev.client_id == holder.id and dev.holder_client_id is None


# ── гость → владелец ─────────────────────────────────────────────────────────

def test_guest_upgrade_moves_all_held_devices_even_over_the_limit(services, make_active_client):
    owner = make_active_client(tg_id=910, name="Вася", device_limit=5)
    a, res = _lend(services, owner, "Телефон", 9910, "Артём")
    b, _ = _lend(services, owner, "Ноутбук", 9910)
    c, _ = _lend(services, owner, "Планшет", 9910)
    guest = res.holder
    services.routing_add_domains(guest.id, "bank.ru")

    created = services.create_client("Артём", 2, "year", 0)
    act = services.activate_client(created.invite_code, 9910)
    assert act.ok and act.upgrade is not None
    up = act.upgrade
    assert up.donor.id == owner.id
    assert [d.name for d in up.moved] == ["Телефон", "Ноутбук", "Планшет"]

    new = act.client
    assert new.tg_id == 9910 and not new.is_guest and new.device_limit == 2
    assert services.db.get_client(guest.id) is None, "гостевой профиль закрыт"
    assert services.db.count_devices(new.id) == 3, "все три — его, «3 из 2» честно"
    assert services.db.count_devices(owner.id) == 0, "слоты дарителю вернулись"
    for d in (a, b, c):
        dev = services.db.get_device(d.device_id)
        assert dev.client_id == new.id and dev.holder_client_id is None
    assert services.routing_domains(new.id) == ["bank.ru"], "список адресов уехал с гостем"
    assert services.db.list_held_devices(new.id) == []


def test_guest_upgrade_recomputes_cascade_blocks_from_the_new_owner(services, make_active_client):
    """Пауза дарителя висела на устройстве битом PAUSED — у нового владельца
    паузы нет, бит снимается; истечение — так же."""
    owner = make_active_client(tg_id=911, period_kind="year")
    a, res = _lend(services, owner, "Телефон", 9911)
    ok, *_ = services.enter_pause(owner.id, 5)
    assert ok
    assert int(services.db.get_device(a.device_id).block_reason) & int(DeviceBlock.PAUSED)
    created = services.create_client("Артём", 2, "year", 0)
    act = services.activate_client(created.invite_code, 9911)
    assert act.ok
    assert not int(services.db.get_device(a.device_id).block_reason) & int(DeviceBlock.PAUSED)


def test_owner_activating_a_second_invite_is_still_refused(services, make_active_client):
    make_active_client(tg_id=912)
    created = services.create_client("Ещё", 1, "year", 0)
    res = services.activate_client(created.invite_code, 912)
    assert not res.ok and res.reason == "already_has_access"


# ── перенос прежних друзей при обновлении ────────────────────────────────────

def test_legacy_friends_become_guest_holders_on_schema_init(tmp_path):
    """БД прежней модели (друг = tg_id на устройстве, колонок kind/holder нет)
    после init_schema: гостевой профиль «Друг» держит устройства, строк active
    в device_friend не остаётся, ожидающий код цел. Идемпотентно."""
    import re
    import sqlite3
    from awgbot.infra import db as dbmod
    old_schema = "\n".join(
        ln for ln in dbmod.SCHEMA.splitlines()
        if not ln.lstrip().startswith("kind ") and "holder_client_id" not in ln)
    old_schema = re.sub(r",(\s*\n\s*\);)", r"\1", old_schema)   # хвостовая запятая после снятой FK
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(old_schema)
    con.execute("INSERT INTO clients (tg_id, name, device_limit, activation_status, "
                "is_service, created_at) VALUES (100, 'Вася', 3, 'active', 0, 'x')")
    oid = con.execute("SELECT id FROM clients WHERE tg_id = 100").fetchone()[0]
    con.execute("INSERT INTO client_subscription (client_id) VALUES (?)", (oid,))
    con.execute("INSERT INTO client_quota (client_id) VALUES (?)", (oid,))
    for i, name in enumerate(("A", "B")):
        con.execute("INSERT INTO devices (client_id, name, public_key, preshared_key, address, "
                    "created_at) VALUES (?, ?, ?, 'psk', ?, ?)",
                    (oid, name, f"pub{i}", f"10.8.1.{i + 2}", f"2026-01-0{i + 1}T00:00:00+03:00"))
        did = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        con.execute("INSERT INTO device_traffic (device_id) VALUES (?)", (did,))
        con.execute("INSERT INTO device_friend (device_id, friend_tg_id, friend_code, friend_status) "
                    "VALUES (?, 555, NULL, 'active')", (did,))
    con.execute("INSERT INTO devices (client_id, name, public_key, preshared_key, address, "
                "created_at) VALUES (?, 'C', 'pub9', 'psk', '10.8.1.9', 'x')", (oid,))
    did = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute("INSERT INTO device_traffic (device_id) VALUES (?)", (did,))
    con.execute("INSERT INTO device_friend (device_id, friend_tg_id, friend_code, friend_status) "
                "VALUES (?, NULL, 'Fpending', 'pending')", (did,))
    con.commit()
    assert "holder_client_id" not in {r[1] for r in con.execute("PRAGMA table_info(devices)")}
    con.close()

    db = Database(path)
    db.init_schema()                                       # обновление
    guest = db.get_client_by_tg(555)
    assert guest is not None and guest.is_guest and guest.name == "Друг"
    held = db.list_held_devices(guest.id)
    assert [d.name for d in held] == ["A", "B"] and all(d.client_id == oid for d in held)
    n = db._connection().execute(
        "SELECT COUNT(*) AS n FROM device_friend WHERE friend_status = 'active'").fetchone()["n"]
    assert n == 0
    pending = [d for d in db.list_devices(oid) if d.name == "C"][0]
    assert pending.friend_status == "pending" and pending.friend_code == "Fpending"
    db.init_schema()                                       # идемпотентно
    assert db.get_client_by_tg(555).id == guest.id and len(db.get_devices_by_friend_tg(555)) == 2


def test_admin_cannot_take_a_friend_code(services, make_active_client):
    owner = make_active_client(tg_id=913)
    dc = services.add_device(owner.id, "A")
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=config.ADMIN_ID)
    assert not res.ok and res.reason == "already_user"
