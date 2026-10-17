"""
Задача сроков на записи с давно прошедшим period_end (страховка правила
«восстановление не оживляет истёкшее»): истекает и шлёт ровно одно «истекла»
каждому адресату — пороги «скоро истечёт», помеченные раньше, не мешают; на
записи, уже истёкшей пересчётом после восстановления, — ничего.
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.core.blocks import ClientBlock
from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def _stale(services, client_id: int, *, status: str = "active") -> None:
    con = services.db._connection()
    con.execute("UPDATE client_subscription SET period_start = ?, period_end = ?, status = ?, "
                "notified_thresholds = '43200,10080,1440' WHERE client_id = ?",
                (timeutil.to_iso(timeutil.shift(timeutil.now(), -400 * 86400)),
                 timeutil.to_iso(timeutil.shift(timeutil.now(), -30 * 86400)), status, client_id))
    con.commit()


def test_a_long_past_end_with_marked_thresholds_expires_once(services, make_active_client):
    ivan = make_active_client(name="Иван", tg_id=2001)
    services.add_device(ivan.id, "Телефон")
    _stale(services, ivan.id)
    notes = services.check_expiry()
    assert sorted(n.tg_id for n in notes) == sorted([2001, config.ADMIN_ID]), notes
    c = services.db.get_client(ivan.id)
    assert c.status == "expired" and c.block_reason & int(ClientBlock.EXPIRY)
    assert services.check_expiry() == [], "«истекла» ушло второй раз"


def test_an_already_expired_record_sends_nothing(services, make_active_client):
    ivan = make_active_client(name="Иван", tg_id=2001)
    _stale(services, ivan.id, status="expired")
    assert services.check_expiry() == []
