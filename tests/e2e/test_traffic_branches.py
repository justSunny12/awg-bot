"""E2E: недобитые ветки check_traffic_limits — переданное устройство сверх лимита,
самопревышение админ-клиента, клиентский порог предупреждения.
"""
import pytest

from awgbot.core import config
from awgbot.core import settings
from awgbot.domain.services import BYTES_PER_GB

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _befriend(services, owner_id, friend_tg, name="d"):
    dc = services.add_device(owner_id, name)
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=friend_tg)
    return dc


def test_friend_device_over_limit_notifies_host_and_friend(services, fake_awg, make_active_client):
    owner = make_active_client(tg_id=1200)
    dc = _befriend(services, owner.id, friend_tg=91200)
    services.set_device_traffic_limit(dc.device_id, 100)
    services.db.add_traffic(dc.device_id, 70, 60)
    notes = services.check_traffic_limits()
    targets = {n.tg_id for n in notes}
    assert 1200 in targets and 91200 in targets


def test_admin_client_cannot_be_traffic_limited(services, fake_awg, make_active_client):
    # Клиент админа НЕ ограничивается: set_client_traffic_limit — no-op для него.
    admin = make_active_client(tg_id=ADMIN, name="Админ")
    services.add_device(admin.id, "d")
    services.set_client_traffic_limit(admin.id, 100)
    fresh = services.db.get_client(admin.id)
    assert int(fresh.traffic_limit) == 0        # остался безлимитным (guard сработал)


def test_client_warn_threshold_notice(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=1201)
    dc = services.add_device(client.id, "d")
    services.set_client_traffic_limit(client.id, 100 * BYTES_PER_GB)
    warn = settings.get_int("limits.traffic_warn_percent", 80)
    used = (warn + 5) * BYTES_PER_GB
    services.db.add_traffic(dc.device_id, used, 0)
    notes = services.check_traffic_limits()
    assert any(n.tg_id == 1201 for n in notes)
    fresh = services.db.get_client(client.id)
    assert fresh.bonus_granted_month == 0
