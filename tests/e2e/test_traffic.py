"""E2E: лимиты трафика — лимит устройства, доп.квота, каскадный блок клиента."""
import pytest

from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.core import config
from awgbot.domain.services import BYTES_PER_GB

pytestmark = pytest.mark.e2e


def test_device_limit_exceeded_blocks_device(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=600)
    dc = services.add_device(client.id, "d")
    services.set_device_traffic_limit(dc.device_id, 100)
    services.db.add_traffic(dc.device_id, 60, 60)          # 120 > 100
    notes = services.check_traffic_limits()
    dev = services.db.get_device(dc.device_id)
    assert int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER)
    assert dc.address in fake_awg.blocked
    assert any(n.tg_id == 600 for n in notes)


def test_device_warn_at_80_percent(services, make_active_client):
    client = make_active_client(tg_id=601)
    dc = services.add_device(client.id, "d")
    services.set_device_traffic_limit(dc.device_id, 100)
    services.db.add_traffic(dc.device_id, 50, 35)          # 85 → ≥80%, но <100
    notes = services.check_traffic_limits()
    dev = services.db.get_device(dc.device_id)
    assert int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER) == 0   # ещё не заблокирован
    assert any(n.tg_id == 601 for n in notes)              # но предупреждён


def test_client_total_first_over_grants_bonus(services, make_active_client):
    client = make_active_client(tg_id=602)
    dc = services.add_device(client.id, "d")
    services.set_client_traffic_limit(client.id, 100)
    services.db.add_traffic(dc.device_id, 70, 60)          # 130 > 100, доп.квоты ещё не было
    services.check_traffic_limits()
    fresh = services.db.get_client(client.id)
    assert fresh.bonus_granted_month == 1                  # выдана разовая доп.квота
    assert fresh.bonus_bytes == config.TRAFFIC_BONUS_GB * BYTES_PER_GB
    assert int(fresh.block_reason) & int(ClientBlock.TRAFFIC_CLIENT) == 0   # ещё не блок


def test_client_cascade_block_after_bonus_exhausted(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=603)
    dc = services.add_device(client.id, "d")
    services.set_client_traffic_limit(client.id, 100)
    # эмулируем «доп.квота уже выдана и исчерпана»
    services.db.update_client_fields(client.id, bonus_granted_month=1, bonus_bytes=0)
    services.db.add_traffic(dc.device_id, 70, 60)          # 130 > 100
    services.check_traffic_limits()
    fresh = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert int(fresh.block_reason) & int(ClientBlock.TRAFFIC_CLIENT)
    assert int(dev.block_reason) & int(DeviceBlock.TRAFFIC_CLIENT)
    assert dc.address in fake_awg.blocked

    # поднятие клиентского лимита → снятие каскада с клиента и устройств
    services.set_client_traffic_limit(client.id, 0)        # безлимит
    fresh = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert int(fresh.block_reason) & int(ClientBlock.TRAFFIC_CLIENT) == 0
    assert int(dev.block_reason) & int(DeviceBlock.TRAFFIC_CLIENT) == 0
    assert dc.address not in fake_awg.blocked


def test_client_and_device_limits_independent(services, make_active_client):
    # каскадный TRAFFIC_CLIENT и собственный TRAFFIC_USER — разные биты
    client = make_active_client(tg_id=604)
    dc = services.add_device(client.id, "d")
    services._device_set_block(dc.device_id, DeviceBlock.TRAFFIC_USER)
    services._device_set_block(dc.device_id, DeviceBlock.TRAFFIC_CLIENT)
    # поднятие клиентского лимита снимает только каскад, не свой лимит устройства
    services.set_client_traffic_limit(client.id, 0)
    dev = services.db.get_device(dc.device_id)
    assert int(dev.block_reason) & int(DeviceBlock.TRAFFIC_CLIENT) == 0
    assert int(dev.block_reason) & int(DeviceBlock.TRAFFIC_USER)   # остался
