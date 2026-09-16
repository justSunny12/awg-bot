"""Integration: friend-поток (роль invited) на уровне services.

Полный жизненный цикл гостевого доступа: пометить устройство гостевым →
активировать код другом → мультидружба → защита владения → перевыдача/отзыв.
БД настоящая, awg-слой фейковый.
"""
import pytest

from awgbot.core import config
from awgbot.core.enums import FriendStatus
from awgbot.domain.services import ServiceError

pytestmark = pytest.mark.integration


# ── make_device_friendly ─────────────────────────────────────────────────────
def test_make_friendly_sets_pending_and_returns_code(services, make_active_client):
    client = make_active_client(tg_id=800)
    dc = services.add_device(client.id, "d")
    code = services.make_device_friendly(dc.device_id)
    assert code.startswith("F")
    dev = services.db.get_device(dc.device_id)
    assert dev.friend_status == FriendStatus.PENDING
    assert dev.friend_code == code
    assert dev.friend_tg_id is None


def test_make_friendly_rejects_device_without_key(services, make_active_client):
    # ключа у бота нет → передать другу нечего
    client = make_active_client(tg_id=801)
    did = services.db.create_device(client.id, "app-dev", "APPPUB", "PSK", "10.8.0.9", private_key=None)
    with pytest.raises(ServiceError):
        services.make_device_friendly(did)


def test_make_friendly_rejects_already_active(services, make_active_client):
    owner = make_active_client(tg_id=802)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=90802)             # друг подключился
    with pytest.raises(ServiceError):
        services.make_device_friendly(dc.device_id)         # уже управляет друг


# ── reissue_friend_code ──────────────────────────────────────────────────────
def test_reissue_replaces_pending_code(services, make_active_client):
    client = make_active_client(tg_id=803)
    dc = services.add_device(client.id, "d")
    first = services.make_device_friendly(dc.device_id)
    second = services.reissue_friend_code(dc.device_id)
    assert second != first
    assert services.db.get_device_by_friend_code(first) is None    # старый недействителен
    assert services.db.get_device_by_friend_code(second).id == dc.device_id


def test_reissue_rejected_after_activation(services, make_active_client):
    owner = make_active_client(tg_id=804)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=90804)
    with pytest.raises(ServiceError):
        services.reissue_friend_code(dc.device_id)          # активированное перевыдать нельзя


# ── activate_friend ──────────────────────────────────────────────────────────
def test_activate_friend_happy(services, make_active_client):
    owner = make_active_client(tg_id=805)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    res = services.activate_friend(code, tg_id=90805)
    assert res.ok and res.device_id == dc.device_id
    dev = services.db.get_device(dc.device_id)
    assert dev.friend_status == FriendStatus.ACTIVE
    assert dev.friend_tg_id == 90805
    assert dev.friend_code is None                          # код погашен
    holder = services.db.get_client_by_tg(90805)
    assert holder.is_guest and services.db.list_held_devices(holder.id)[0].id == dc.device_id


def test_activate_friend_invalid_code(services):
    res = services.activate_friend("Fnope", tg_id=90806)
    assert not res.ok and res.reason == "invalid"


def test_activate_friend_code_not_pending(services, make_active_client):
    owner = make_active_client(tg_id=807)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=90807)             # уже активирован
    res = services.activate_friend(code, tg_id=90808)       # второй пытается тем же кодом
    assert not res.ok and res.reason == "invalid"


def test_activate_friend_by_existing_client_holds_without_slot(services, make_active_client):
    """Обычный клиент принимает чужое устройство: держит его своим профилем,
    слот и квота — у дарителя (docs/guest-role.md)."""
    owner = make_active_client(tg_id=809)
    other = make_active_client(tg_id=90809, device_limit=1)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    res = services.activate_friend(code, tg_id=other.tg_id)
    assert res.ok and res.holder.id == other.id and not res.holder.is_guest
    dev = services.db.get_device(dc.device_id)
    assert dev.client_id == owner.id and dev.holder_client_id == other.id
    assert services.db.count_devices(other.id) == 0, "чужое устройство заняло слот держателя"
    assert [d.id for d in services.db.list_held_devices(other.id)] == [dc.device_id]


def test_activate_friend_refuses_own_device_and_second_donor(services, make_active_client):
    """Своё устройство держать незачем; устройства от второго дарителя — отказ,
    код не сгорает (правило одного дарителя)."""
    owner = make_active_client(tg_id=814)
    other_owner = make_active_client(tg_id=815)
    own = services.add_device(owner.id, "своё")
    res = services.activate_friend(services.make_device_friendly(own.device_id), tg_id=owner.tg_id)
    assert not res.ok and res.reason == "own_device"

    first = services.add_device(owner.id, "первое")
    res = services.activate_friend(services.make_device_friendly(first.device_id), tg_id=90814,
                                   tg_name="Артём")
    assert res.ok and res.holder.is_guest and res.holder.name == "Артём"
    foreign = services.add_device(other_owner.id, "чужое")
    code = services.make_device_friendly(foreign.device_id)
    res = services.activate_friend(code, tg_id=90814)
    assert not res.ok and res.reason == "other_donor"
    assert res.donor.id == owner.id and [d.name for d in res.held] == ["первое"]
    assert services.db.get_device_by_friend_code(code) is not None, "код сгорел"
    assert services.db.get_device(foreign.device_id).holder_client_id is None


def test_activate_friend_rejects_admin(services, make_active_client):
    owner = make_active_client(tg_id=810)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    res = services.activate_friend(code, tg_id=config.ADMIN_ID)
    assert not res.ok and res.reason == "already_user"


# ── мультидружба + защита владения ───────────────────────────────────────────
def test_multi_friendship_lists_all(services, make_active_client):
    """Несколько устройств от ОДНОГО владельца — все у держателя."""
    owner = make_active_client(tg_id=811, device_limit=3)
    da = services.add_device(owner.id, "a")
    db_ = services.add_device(owner.id, "b")
    friend = 90811
    services.activate_friend(services.make_device_friendly(da.device_id), tg_id=friend)
    services.activate_friend(services.make_device_friendly(db_.device_id), tg_id=friend)
    guest = services.db.get_client_by_tg(friend)
    devs = services.db.list_held_devices(guest.id)
    assert {d.id for d in devs} == {da.device_id, db_.device_id}
    assert guest.is_guest and services.db.list_clients() == [c for c in services.db.list_clients() if not c.is_guest]


def test_held_devices_are_scoped_to_the_holder(services, make_active_client):
    """Чужой device_id в колбэке отсекается тем, что выборка идёт по держателю:
    у другого профиля этого устройства в списке нет."""
    owner = make_active_client(tg_id=813)
    dc = services.add_device(owner.id, "d")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=90813)
    other = make_active_client(tg_id=90814)
    assert [d.id for d in services.db.list_held_devices(other.id)] == []
    holder = services.db.get_client_by_tg(90813)
    assert [d.id for d in services.db.list_held_devices(holder.id)] == [dc.device_id]
