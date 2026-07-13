"""Unit: awgbot.core.models — плоские property и None-логика ленивых процессов."""
import pytest

from awgbot.core import models

pytestmark = pytest.mark.unit


def _client(**kw):
    base = dict(id=1, tg_id=100, name="Тест", device_limit=3, block_reason=0,
                is_service=0, activation_status="active", invite_code=None,
                created_at="2026-01-01T00:00:00+03:00")
    base.update(kw)
    return models.Client(**base)


# ── делегирование в под-объекты ──────────────────────────────────────────────
def test_subscription_delegation():
    c = _client(subscription=models.Subscription(period_end="2027-01-01T00:00:00+03:00",
                                                 period_kind="year", status="active"))
    assert c.period_end == "2027-01-01T00:00:00+03:00"
    assert c.period_kind == "year"
    assert c.status == "active"


def test_quota_delegation():
    c = _client(quota=models.TrafficQuota(limit=1000, bonus_bytes=50, bonus_granted_month=1))
    assert c.traffic_limit == 1000
    assert c.bonus_bytes == 50
    assert c.bonus_granted_month == 1


# ── ленивые процессы: None ⇔ неактивно ───────────────────────────────────────
def test_grace_none_defaults():
    c = _client()
    assert c.grace is None
    assert c.grace_used == 0
    assert c.grace_pending_cut == 0


def test_grace_present():
    c = _client(grace=models.GraceState(used=1, pending_cut=1209600))
    assert c.grace_used == 1
    assert c.grace_pending_cut == 1209600


def test_pause_none_not_paused():
    c = _client()
    assert c.pause is None
    assert c.is_paused is False
    assert c.pause_mode is None
    assert c.pause_used_days == 0


def test_pause_active():
    c = _client(pause=models.PauseState(active_since="2026-05-01T12:00:00+03:00",
                                        reserved_days=14, used_days=0, mode="user"))
    assert c.is_paused is True
    assert c.pause_mode == "user"
    assert c.pause_reserved_days == 14


def test_pause_materialized_but_inactive():
    # объект есть ради накопленного счётчика, но active_since пуст → НЕ на паузе
    c = _client(pause=models.PauseState(active_since=None, used_days=5, mode="user"))
    assert c.is_paused is False
    assert c.pause_used_days == 5


# ── устройство ───────────────────────────────────────────────────────────────
def _device(**kw):
    base = dict(id=1, client_id=1, name="Телефон", private_key="PK",
                public_key="PUB", preshared_key="PSK", address="10.8.1.4",
                block_reason=0, created_at="2026-01-01T00:00:00+03:00")
    base.update(kw)
    return models.Device(**base)


def test_device_traffic_and_friend_delegation():
    d = _device(traffic=models.DeviceTraffic(limit=500, rx_month=10, tx_month=20,
                                             last_handshake=1720000000, missing_count=1))
    assert d.traffic_limit == 500
    assert d.traffic_rx_month == 10 and d.traffic_tx_month == 20
    assert d.last_handshake == 1720000000
    assert d.missing_count == 1
    assert d.friend is None and d.friend_tg_id is None and d.friend_status is None


def test_device_friend_present():
    d = _device(friend=models.Friend(tg_id=777, code="abc", status="active"))
    assert d.friend_tg_id == 777
    assert d.friend_code == "abc"
    assert d.friend_status == "active"
