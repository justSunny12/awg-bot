"""
Смена лимита в окне переезда профилей — решение о снятии блока по паре.

Стережём: проверка лимита считает расход устройства суммой по паре (исходная
строка + двойник на втором интерфейсе) и профиля — по всем строкам без шлюза,
а смена лимита смотрела на одну строку и на видимые строки: блок снимался,
хотя пара лимит уже превысила (следующий тик ставил его снова — дребезг
доступа и уведомлений), и не снимался при новом лимите выше суммы — если
расход лежал на строке, которой сеттер не видел.
"""
from __future__ import annotations

import pytest

from awgbot.core.blocks import ClientBlock, DeviceBlock
from tests.integration._migration_common import _twin

pytestmark = pytest.mark.integration

GB = 2 ** 30


def _used(services, device_id: int, gb: int) -> None:
    with services.db._tx() as c:
        c.execute("UPDATE device_traffic SET traffic_rx_month = ?, traffic_tx_month = 0 WHERE device_id = ?",
                  (gb * GB, device_id))


@pytest.fixture()
def pair(services, mig, make_active_client):
    c = make_active_client(name="p", tg_id=7201)
    d = services.add_device(c.id, "Телефон")
    services.migration_start()
    twin = _twin(services, d.device_id)
    _used(services, d.device_id, 3)
    _used(services, twin.id, 3)                            # пара — 6 ГБ
    return c, d.device_id, twin.id


def _bits(services, *ids):
    return [int(services.db.get_device(i).block_reason) for i in ids]


def test_raising_the_device_limit_below_the_pair_sum_keeps_the_block(services, pair):
    _c, old, twin = pair
    services.set_device_traffic_limit(old, 2 * GB)
    services.check_traffic_limits()
    assert all(b & int(DeviceBlock.TRAFFIC_USER) for b in _bits(services, old, twin))
    services.set_device_traffic_limit(twin, 4 * GB)        # строка — 3 ГБ, пара — 6: всё ещё сверх
    assert all(b & int(DeviceBlock.TRAFFIC_USER) for b in _bits(services, old, twin))
    services.set_device_traffic_limit(twin, 7 * GB)
    assert not any(b & int(DeviceBlock.TRAFFIC_USER) for b in _bits(services, old, twin))


def test_raising_the_profile_limit_counts_every_row(services, pair):
    c, old, twin = pair
    services.set_client_traffic_limit(c.id, 2 * GB)
    services.db.update_client_fields(c.id, bonus_granted_month=1)   # бонус месяца уже был — блок
    services.check_traffic_limits()
    assert int(services.db.get_client(c.id).block_reason) & int(ClientBlock.TRAFFIC_CLIENT)
    services.set_client_traffic_limit(c.id, 5 * GB)        # видимая строка — 3, все строки — 6
    assert int(services.db.get_client(c.id).block_reason) & int(ClientBlock.TRAFFIC_CLIENT)
    services.set_client_traffic_limit(c.id, 7 * GB)
    assert not int(services.db.get_client(c.id).block_reason) & int(ClientBlock.TRAFFIC_CLIENT)
    assert not any(b & int(DeviceBlock.TRAFFIC_CLIENT) for b in _bits(services, old, twin))
