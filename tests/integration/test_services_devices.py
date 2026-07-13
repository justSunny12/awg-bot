"""Integration: awgbot.domain.services — клиенты, устройства, блокировки, лимиты.

БД настоящая (временная SQLite), awg-слой — фейковый (fixture fake_awg).
"""
import pytest

from awgbot.core.blocks import DeviceBlock
from awgbot.domain.services import LimitReached, ServiceError

pytestmark = pytest.mark.integration


# ── создание и активация клиента ─────────────────────────────────────────────
def test_create_client_returns_invite(services):
    created = services.create_client("Клиент", 3, "year", traffic_limit=0)
    assert created.invite_code.startswith("C") and len(created.invite_code) == 12
    assert created.period_end is not None
    row = services.db.get_client(created.client_id)
    assert row.activation_status == "pending"


def test_create_client_never_period_has_no_end(services):
    created = services.create_client("Бессрочный", 1, "never")
    assert created.period_end is None


def test_create_client_bad_period_raises(services):
    with pytest.raises(ServiceError):
        services.create_client("X", 1, "decade")


def test_activate_ok(services):
    created = services.create_client("A", 2, "year")
    res = services.activate_client(created.invite_code, tg_id=42)
    assert res.ok and res.reason == "ok"
    assert res.client.tg_id == 42 and res.client.activation_status == "active"


def test_activate_invalid_code(services):
    res = services.activate_client("Cnonexistent0", tg_id=42)
    assert not res.ok and res.reason == "invalid"


def test_activate_already_has_access(services, make_active_client):
    make_active_client(tg_id=42)
    other = services.create_client("B", 1, "year")
    res = services.activate_client(other.invite_code, tg_id=42)
    assert not res.ok and res.reason == "already_has_access"


def test_regenerate_invite_only_pending(services, make_active_client):
    created = services.create_client("A", 1, "year")
    code2 = services.regenerate_invite(created.client_id)
    assert code2 != created.invite_code
    active = make_active_client(tg_id=7)
    with pytest.raises(ServiceError):
        services.regenerate_invite(active.id)


# ── добавление устройства (поток ключи→IP→БД→peer→конфиг) ────────────────────
def test_add_device_full_flow(services, fake_awg, make_active_client):
    client = make_active_client(device_limit=3)
    dc = services.add_device(client.id, "Телефон")
    assert dc.address == "10.8.1.1"                       # первый свободный из пула
    assert dc.vpn.startswith("vpn://") and "[Interface]" in dc.conf
    dev = services.db.get_device(dc.device_id)
    assert dev.name == "Телефон" and dev.is_managed
    assert dev.public_key in fake_awg.peers               # peer применён в «контейнере»
    assert fake_awg.clientstable.get(dev.public_key) == "Телефон"


def test_add_device_allocates_sequential_ips(services, make_active_client):
    client = make_active_client(device_limit=5)
    a = services.add_device(client.id, "d1")
    b = services.add_device(client.id, "d2")
    assert (a.address, b.address) == ("10.8.1.1", "10.8.1.2")


def test_add_device_respects_limit(services, make_active_client):
    client = make_active_client(device_limit=1)
    services.add_device(client.id, "d1")
    with pytest.raises(LimitReached):
        services.add_device(client.id, "d2")


def test_add_device_service_client_bypasses_limit(services):
    sid = services.db.get_service_client_id()
    # у служебного клиента лимит не применяется
    services.add_device(sid, "svc1")
    services.add_device(sid, "svc2")
    assert services.db.count_devices(sid) == 2


# ── перевыпуск конфига ───────────────────────────────────────────────────────
def test_generate_config_bot_device(services, make_active_client):
    client = make_active_client()
    dc = services.add_device(client.id, "d")
    cfg = services.generate_config(dc.device_id)
    assert cfg["vpn"].startswith("vpn://") and "PrivateKey" in cfg["conf"]


def test_generate_config_app_device_forbidden(services, make_active_client):
    client = make_active_client()
    # app-устройство: приватного ключа нет
    did = services.db.create_device(client.id, "app", "PUBapp", "PSK", "10.8.1.9", private_key=None)
    with pytest.raises(ServiceError):
        services.generate_config(did)


# ── удаление устройства ──────────────────────────────────────────────────────
def test_remove_device_removes_peer_and_row(services, fake_awg, make_active_client):
    client = make_active_client()
    dc = services.add_device(client.id, "d")
    pub = services.db.get_device(dc.device_id).public_key
    services.remove_device(dc.device_id)
    assert services.db.get_device(dc.device_id) is None
    assert pub not in fake_awg.peers


# ── ручные блокировки устройства (маска + физический DROP) ────────────────────
def test_manual_block_sets_bit_and_drop(services, fake_awg, make_active_client):
    client = make_active_client()
    dc = services.add_device(client.id, "d")
    services.block_device_manual(dc.device_id, DeviceBlock.ADMIN_NOTIFIED, notify=False)
    dev = services.db.get_device(dc.device_id)
    assert int(dev.block_reason) & int(DeviceBlock.ADMIN_NOTIFIED)
    assert dc.address in fake_awg.blocked                 # DROP наложен


def test_manual_unblock_clears_drop_when_no_reasons_left(services, fake_awg, make_active_client):
    client = make_active_client()
    dc = services.add_device(client.id, "d")
    services.block_device_manual(dc.device_id, DeviceBlock.ADMIN_NOTIFIED, notify=False)
    services.unblock_device_manual(dc.device_id, DeviceBlock.ADMIN_NOTIFIED, notify=False)
    assert services.db.get_device(dc.device_id).block_reason == 0
    assert dc.address not in fake_awg.blocked             # DROP снят


def test_manual_block_notify_returns_owner_notification(services, make_active_client):
    client = make_active_client(tg_id=333)
    dc = services.add_device(client.id, "d")
    notes = services.block_device_manual(dc.device_id, DeviceBlock.ADMIN_NOTIFIED, notify=True)
    assert any(n.tg_id == 333 for n in notes)
    silent = services.add_device(client.id, "d2")
    quiet = services.block_device_manual(silent.device_id, DeviceBlock.ADMIN_SILENT, notify=False)
    assert quiet == []                                    # тихий блок — без уведомлений


# ── лимит трафика устройства: снятие бита при поднятии лимита ─────────────────
def test_raise_device_limit_clears_traffic_user_block(services, fake_awg, make_active_client):
    client = make_active_client()
    dc = services.add_device(client.id, "d")
    services.set_device_traffic_limit(dc.device_id, 100)
    services.db.add_traffic(dc.device_id, 60, 60)         # 120 > 100 → над лимитом
    services._device_set_block(dc.device_id, DeviceBlock.TRAFFIC_USER)
    assert dc.address in fake_awg.blocked
    services.set_device_traffic_limit(dc.device_id, 0)    # безлимит → снять бит
    assert not (int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.TRAFFIC_USER))
    assert dc.address not in fake_awg.blocked
