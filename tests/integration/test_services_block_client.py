"""Integration: ручной админ-блок КЛИЕНТА (block_client_manual / unblock_client_manual).

Проверяем каскад бита на устройства, интеграцию с приостановкой подписки
(pause_days None/0/N), уведомления клиенту и друзьям, и что снятие блока с
активной админ-паузой закрывает её через exit_pause (пересчёт периода).
"""
import pytest

from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.core.enums import PauseMode

pytestmark = pytest.mark.integration


def _friended_device(services, owner_id, friend_tg, name="d"):
    dc = services.add_device(owner_id, name)
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=friend_tg)
    return dc


# ── блок ─────────────────────────────────────────────────────────────────────
def test_block_client_cascades_bit_to_devices(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1000)
    dc = services.add_device(client.id, "d")
    services.block_client_manual(client.id, ClientBlock.ADMIN_NOTIFIED, notify=False)
    fresh = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert int(fresh.block_reason) & int(ClientBlock.ADMIN_NOTIFIED)
    assert int(dev.block_reason) & int(DeviceBlock.ADMIN_NOTIFIED)   # каскад тем же битом
    assert dc.address in fake_awg.blocked


def test_block_client_notify_reaches_client_and_friend(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1001)
    dc = _friended_device(services, client.id, friend_tg=91001)
    notes = services.block_client_manual(client.id, ClientBlock.ADMIN_NOTIFIED, notify=True)
    targets = {n.tg_id for n in notes}
    assert 1001 in targets and 91001 in targets


def test_block_client_silent_sends_nothing(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1002)
    _friended_device(services, client.id, friend_tg=91002)
    notes = services.block_client_manual(client.id, ClientBlock.ADMIN_SILENT, notify=False)
    assert notes == []


def test_block_client_is_service_noop(services, fake_awg):
    service_id = services.ensure_admin_client()  # админ-клиент существует, но нам нужен служебный
    svc = services.db.get_service_client_id()
    notes = services.block_client_manual(svc, ClientBlock.ADMIN_NOTIFIED, notify=True)
    assert notes == []


# ── блок с приостановкой подписки ────────────────────────────────────────────
def test_block_client_open_pause_suspends_subscription(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1003, period_kind="year")
    dc = services.add_device(client.id, "d")
    services.block_client_manual(client.id, ClientBlock.ADMIN_NOTIFIED, notify=False, pause_days=0)
    fresh = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert fresh.is_paused and fresh.pause_mode == PauseMode.ADMIN_OPEN
    assert fresh.period_end is None                          # temp-бессрочная
    assert int(fresh.block_reason) & int(ClientBlock.PAUSED)
    assert int(dev.block_reason) & int(DeviceBlock.PAUSED)   # PAUSED каскадит тоже


def test_block_client_fixed_pause_shifts_period(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1004, period_kind="year")
    before = services.db.get_client(client.id).period_end
    services.block_client_manual(client.id, ClientBlock.ADMIN_SILENT, notify=False, pause_days=10)
    fresh = services.db.get_client(client.id)
    assert fresh.pause_mode == PauseMode.ADMIN_FIXED
    assert fresh.period_end is not None and fresh.period_end > before   # сдвинут вперёд


# ── снятие ───────────────────────────────────────────────────────────────────
def test_unblock_client_clears_cascade_and_notifies(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1005)
    dc = _friended_device(services, client.id, friend_tg=91005)
    services.block_client_manual(client.id, ClientBlock.ADMIN_NOTIFIED, notify=False)
    notes = services.unblock_client_manual(client.id, ClientBlock.ADMIN_NOTIFIED, notify=True)
    fresh = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert int(fresh.block_reason) == 0
    assert int(dev.block_reason) == 0
    assert dc.address not in fake_awg.blocked
    targets = {n.tg_id for n in notes}
    assert 1005 in targets and 91005 in targets              # клиенту и другу — «разблокировано»


def test_unblock_client_closes_open_pause_and_restores_period(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1006, period_kind="year")
    original_end = services.db.get_client(client.id).period_end
    services.block_client_manual(client.id, ClientBlock.ADMIN_NOTIFIED, notify=False, pause_days=0)
    assert services.db.get_client(client.id).period_end is None    # приостановлена
    services.unblock_client_manual(client.id, ClientBlock.ADMIN_NOTIFIED, notify=False)
    fresh = services.db.get_client(client.id)
    assert not fresh.is_paused
    assert int(fresh.block_reason) == 0                      # и админ-бит, и PAUSED сняты
    assert fresh.period_end is not None                      # период восстановлен из snapshot


def test_unblock_client_partial_keeps_other_reason(services, fake_awg, make_active_client):
    # снятие одного ручного бита не разблокирует, если остаётся второй
    client = make_active_client(tg_id=1007)
    services.block_client_manual(client.id, ClientBlock.ADMIN_SILENT, notify=False)
    services.block_client_manual(client.id, ClientBlock.ADMIN_NOTIFIED, notify=False)
    notes = services.unblock_client_manual(client.id, ClientBlock.ADMIN_SILENT, notify=True)
    fresh = services.db.get_client(client.id)
    assert int(fresh.block_reason) & int(ClientBlock.ADMIN_NOTIFIED)  # второй бит цел
    assert int(fresh.block_reason) & int(ClientBlock.ADMIN_SILENT) == 0
    assert notes == []                                       # не разблокирован → клиенту молчим
