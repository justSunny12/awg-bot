"""E2E: жизненный цикл клиента — инвайт→активация→устройство→истечение→продление."""
import datetime

import pytest

from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.core.enums import SubStatus
from awgbot.util import timeutil

pytestmark = pytest.mark.e2e


def _expire(db, client_id):
    """Сдвинуть период клиента в прошлое (для проверки истечения)."""
    db.update_client_fields(
        client_id,
        period_start="2020-01-01T00:00:00+03:00",
        period_end="2020-02-01T00:00:00+03:00",
        status=SubStatus.ACTIVE)


def test_full_happy_path_expire_then_extend(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=500, period_kind="year", device_limit=3)
    dc = services.add_device(client.id, "Телефон")
    assert fake_awg.peers and dc.address not in fake_awg.blocked

    # истечение → блокировка устройства и клиента, DROP наложен
    _expire(services.db, client.id)
    notes = services.check_expiry()
    fresh = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert fresh.status == SubStatus.EXPIRED
    assert int(fresh.block_reason) & int(ClientBlock.EXPIRY)
    assert int(dev.block_reason) & int(DeviceBlock.EXPIRY)
    assert dc.address in fake_awg.blocked
    assert any(n.tg_id == 500 for n in notes)              # клиенту — уведомление

    # продление → снятие EXPIRY, DROP снят, статус active
    result = services.extend_period(client.id, "year", keep_remainder=False)
    fresh = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert fresh.status == SubStatus.ACTIVE
    assert int(fresh.block_reason) & int(ClientBlock.EXPIRY) == 0
    assert int(dev.block_reason) & int(DeviceBlock.EXPIRY) == 0
    assert dc.address not in fake_awg.blocked
    assert result.new_end is not None


def test_add_device_to_expired_client_is_blocked(services, fake_awg, make_active_client):
    client = make_active_client(period_kind="year")
    _expire(services.db, client.id)
    services.check_expiry()
    dc = services.add_device(client.id, "Новый")             # добавлен уже истёкшему
    dev = services.db.get_device(dc.device_id)
    assert int(dev.block_reason) & int(DeviceBlock.EXPIRY)
    assert dc.address in fake_awg.blocked


def test_expiry_threshold_notification_with_grace_offer(services, make_active_client):
    client = make_active_client(tg_id=501, period_kind="year")
    now = timeutil.now()
    # период широкий, до конца ~1 час → пересекает порог «2 часа»
    services.db.update_client_fields(
        client.id,
        period_start=timeutil.to_iso(now - datetime.timedelta(days=300)),
        period_end=timeutil.to_iso(now + datetime.timedelta(minutes=60)),
        status=SubStatus.ACTIVE)
    notes = services.check_expiry()
    client_notes = [n for n in notes if n.tg_id == 501]
    assert client_notes, "клиенту должно прийти пред-уведомление об истечении"
    # предложение отсрочки прикреплено (годовой период, grace ещё не брали)
    assert any(n.grace_offer_client_id == client.id for n in client_notes)


def test_expiry_idempotent_second_run_no_duplicate(services, make_active_client):
    client = make_active_client(tg_id=502, period_kind="year")
    _expire(services.db, client.id)
    first = services.check_expiry()
    second = services.check_expiry()          # уже EXPIRED → повторно не уведомляем
    assert any(n.tg_id == 502 for n in first)
    assert all(n.tg_id != 502 for n in second)
