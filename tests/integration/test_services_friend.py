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


def test_make_friendly_rejects_app_device(services, make_active_client):
    # app-устройство: приватного ключа у бота нет → передать другу нечего
    client = make_active_client(tg_id=801)
    service_id = services.db.get_service_client_id()
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
    assert services.friend_devices(90805)[0].id == dc.device_id


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


def test_activate_friend_rejects_existing_client(services, make_active_client):
    owner = make_active_client(tg_id=809)
    other = make_active_client(tg_id=90809)                 # уже действующий клиент
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    res = services.activate_friend(code, tg_id=other.tg_id)
    assert not res.ok and res.reason == "already_user"


def test_activate_friend_rejects_admin(services, make_active_client):
    owner = make_active_client(tg_id=810)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    res = services.activate_friend(code, tg_id=config.ADMIN_ID)
    assert not res.ok and res.reason == "already_user"


# ── мультидружба + защита владения ───────────────────────────────────────────
def test_multi_friendship_lists_all(services, make_active_client):
    owner_a = make_active_client(tg_id=811)
    owner_b = make_active_client(tg_id=812)
    da = services.add_device(owner_a.id, "a")
    db_ = services.add_device(owner_b.id, "b")
    friend = 90811
    services.activate_friend(services.make_device_friendly(da.device_id), tg_id=friend)
    services.activate_friend(services.make_device_friendly(db_.device_id), tg_id=friend)
    devs = services.friend_devices(friend)
    assert {d.id for d in devs} == {da.device_id, db_.device_id}


def test_friend_device_by_id_ownership_guard(services, make_active_client):
    owner = make_active_client(tg_id=813)
    dc = services.add_device(owner.id, "d")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=90813)
    # чужой tg спрашивает про это устройство → None (защита от чужого device_id)
    assert services.friend_device_by_id(90814, dc.device_id) is None
    assert services.friend_device_by_id(90813, dc.device_id).id == dc.device_id
