"""Unit: awgbot.core.blocks — битмаски причин блокировки и хелперы."""
import pytest

from awgbot.core import blocks
from awgbot.core.blocks import ClientBlock, DeviceBlock

pytestmark = pytest.mark.unit

# Причины, названные одинаково у устройства и клиента. Каскад блокировок
# конвертирует биты «тем же типом», полагаясь на равенство их числовых значений.
_SHARED = ["EXPIRY", "TRAFFIC_USER", "TRAFFIC_CLIENT",
           "ADMIN_SILENT", "ADMIN_NOTIFIED", "USER", "PAUSED"]


@pytest.mark.parametrize("name", _SHARED)
def test_shared_bits_have_equal_values(name):
    # инвариант, на который опирается services при каскаде device<->client
    assert int(DeviceBlock[name]) == int(ClientBlock[name])


def test_traffic_any_is_union():
    assert blocks.DEVICE_TRAFFIC_ANY == (DeviceBlock.TRAFFIC_USER | DeviceBlock.TRAFFIC_CLIENT)


def test_has_add_clear():
    m = 0
    m = blocks.add(m, DeviceBlock.EXPIRY)
    m = blocks.add(m, DeviceBlock.USER)
    assert blocks.has(m, DeviceBlock.EXPIRY) and blocks.has(m, DeviceBlock.USER)
    assert not blocks.has(m, DeviceBlock.ADMIN_SILENT)
    m = blocks.clear(m, DeviceBlock.EXPIRY)
    assert not blocks.has(m, DeviceBlock.EXPIRY)
    assert blocks.has(m, DeviceBlock.USER)


def test_manual_masks():
    assert blocks.DEVICE_MANUAL == (DeviceBlock.ADMIN_SILENT | DeviceBlock.ADMIN_NOTIFIED | DeviceBlock.USER)
    assert blocks.CLIENT_MANUAL == (ClientBlock.ADMIN_SILENT | ClientBlock.ADMIN_NOTIFIED)


# ── видимость: тихий админ-блок скрыт от пользователя ────────────────────────
def test_silent_admin_hidden_from_user():
    mask = int(DeviceBlock.ADMIN_SILENT | DeviceBlock.EXPIRY)
    visible = blocks.visible_to_user_device(mask)
    assert not blocks.has(visible, DeviceBlock.ADMIN_SILENT)
    assert blocks.has(visible, DeviceBlock.EXPIRY)


def test_device_reasons_admin_vs_user():
    mask = int(DeviceBlock.ADMIN_SILENT | DeviceBlock.EXPIRY)
    admin = blocks.device_reasons_ru(mask, for_admin=True)
    user = blocks.device_reasons_ru(mask, for_admin=False)
    assert any("тихо" in r for r in admin)          # админ видит тихий блок
    assert all("тихо" not in r for r in user)       # пользователь — нет
    assert any("истек" in r.lower() for r in user)  # видимую причину видят оба


def test_blocked_marker():
    silent_only = int(DeviceBlock.ADMIN_SILENT)
    assert blocks.blocked_marker_device(silent_only, for_admin=True) == "🛑 "
    assert blocks.blocked_marker_device(silent_only, for_admin=False) == ""   # тихий → пользователю не маркируем
    assert blocks.blocked_marker_device(int(DeviceBlock.EXPIRY), for_admin=False) == "🛑 "
    assert blocks.blocked_marker_device(0, for_admin=True) == ""
