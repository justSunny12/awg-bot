"""E2E: приостановка подписки («в отпуск») и отсрочка «продли на пару недель»."""
import pytest

from awgbot.core import config
from awgbot.core.blocks import ClientBlock, DeviceBlock

pytestmark = pytest.mark.e2e


# ── пауза (только годовой период) ────────────────────────────────────────────
def test_pause_available_days_year(services, make_active_client):
    client = make_active_client(period_kind="year")
    # min(единичный лимит, остаток суммарного, остаток подписки); подписка ~год
    assert services.pause_available_days(client.id) == config.PAUSE_MAX_TOTAL_DAYS


def test_pause_unavailable_on_month(services, make_active_client):
    client = make_active_client(period_kind="month")
    assert services.pause_available_days(client.id) == 0
    ok, reserved, _, _ = services.enter_pause(client.id)
    assert ok is False and reserved == 0


def test_enter_and_exit_pause_blocks_then_restores(services, fake_awg, make_active_client):
    client = make_active_client(period_kind="year")
    dc = services.add_device(client.id, "d")
    end_before = services.db.get_client(client.id).period_end

    ok, reserved, _, _ = services.enter_pause(client.id)
    assert ok and reserved == config.PAUSE_MAX_TOTAL_DAYS
    paused = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert paused.is_paused is True
    assert int(paused.block_reason) & int(ClientBlock.PAUSED)
    assert int(dev.block_reason) & int(DeviceBlock.PAUSED)
    assert dc.address in fake_awg.blocked
    assert paused.period_end > end_before                  # срок сдвинут вперёд на резерв

    ok, actual, new_end, _ = services.exit_pause(client.id, auto=False)
    assert ok
    resumed = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert resumed.is_paused is False
    assert int(resumed.block_reason) & int(ClientBlock.PAUSED) == 0
    assert int(dev.block_reason) & int(DeviceBlock.PAUSED) == 0
    assert dc.address not in fake_awg.blocked


def test_double_pause_rejected(services, make_active_client):
    client = make_active_client(period_kind="year")
    assert services.enter_pause(client.id)[0] is True
    assert services.enter_pause(client.id)[0] is False     # уже на паузе


def test_pause_accumulates_used_days_limit(services, make_active_client):
    client = make_active_client(period_kind="year")
    services.enter_pause(client.id)
    services.exit_pause(client.id, auto=False)
    used = services.db.get_client(client.id).pause_used_days
    assert used >= 1                                       # фактические дни зачтены
    # доступное теперь = min(single, total-used, remaining)
    left = config.PAUSE_MAX_TOTAL_DAYS - used
    assert services.pause_available_days(client.id) == left


# ── отсрочка (grace) ─────────────────────────────────────────────────────────
def test_activate_grace_year_once(services, make_active_client):
    client = make_active_client(period_kind="year")
    end_before = services.db.get_client(client.id).period_end
    ok, new_end = services.activate_grace(client.id, 14)
    assert ok is True and new_end is not None
    fresh = services.db.get_client(client.id)
    assert fresh.grace_used == 1
    assert fresh.period_end > end_before
    # повторно за период — нельзя
    ok2, _ = services.activate_grace(client.id, 14)
    assert ok2 is False


def test_grace_unavailable_on_month(services, make_active_client):
    client = make_active_client(period_kind="month")
    ok, new_end = services.activate_grace(client.id, 14)
    assert ok is False and new_end is None


def test_grace_debt_subtracted_on_extend(services, make_active_client):
    # взяли отсрочку 14 дней → долг фиксируется; при продлении вычитается
    client = make_active_client(period_kind="year")
    services.activate_grace(client.id, 14)
    assert services.db.get_client(client.id).grace_pending_cut == 14 * 86400
    services.extend_period(client.id, "year", keep_remainder=False)
    # новый период — grace сброшен (эпизод закрыт)
    assert services.db.get_client(client.id).grace_used == 0
