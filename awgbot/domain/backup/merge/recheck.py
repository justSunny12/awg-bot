"""recheck.py — часть пакета merge (см. __init__): пересчёт по фактическому
времени того, что встало из бэкапа (другая линия базы), — до того, как пиры
получат доступ. Без сети и без уведомлений: «истекла» по такому пользователю уже
уходило с прежнего сервера.

- подписка, истёкшая к моменту восстановления, — expired и бит EXPIRY
  пользователю и его устройствам; пороги «скоро истечёт», пересечённые к моменту
  восстановления, помечаются отправленными молча — то же делает проход
  check_expiry бота, здесь его уведомления выбрасываются;
- устройство и профиль, чей месячный расход не меньше лимита, — бит
  TRAFFIC_USER / TRAFFIC_CLIENT, как check_traffic_limits, и отметка «уже
  сказано»; только если счётчики бэкапа — этого месяца (иначе их снимет и
  обнулит догон месячного сброса на старте).
"""
from __future__ import annotations

import contextlib

from awgbot.core import config
from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.util import timeutil


@contextlib.contextmanager
def _no_iptables():
    """DROP накладывает restore по списку из итога, а не сервисный слой здесь."""
    from awgbot.infra import awg
    saved = awg.block_ip
    awg.block_ip = lambda ip: None
    try:
        yield
    finally:
        awg.block_ip = saved


def _pair(db, dev_id: int) -> list[int]:
    out = [dev_id]
    for r in db._connection().execute("SELECT id FROM devices WHERE twin_of = ? OR id = "
                                      "(SELECT twin_of FROM devices WHERE id = ?)", (dev_id, dev_id)):
        if int(r[0]) not in out:
            out.append(int(r[0]))
    return out


def traffic(db) -> int:
    """Биты лимитов по счётчикам бэкапа — по правилу check_traffic_limits:
    шлюзы не в счёт; у пары переезда расход — сумма обеих строк против лимита
    пары, старая строка отдельно не судится; тотал профиля — против лимита с
    бонусом; профиль админа не блокируется. Возвращает число заблокированных
    устройств."""
    if (db.get_state("last_monthly_reset") or "") != timeutil.now().strftime("%Y-%m"):
        return 0
    n = 0
    for client in db.list_clients(include_service=False):
        if client.activation_status != "active" or client.tg_id == config.ADMIN_ID:
            continue
        devices = [d for d in db.list_devices(client.id, all_rows=True) if not d.is_gateway]
        by_id = {d.id: d for d in devices}
        paired_old = {d.twin_of for d in devices if d.twin_of is not None}
        for dev in devices:
            if dev.id in paired_old or not dev.traffic_limit:
                continue
            used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
            mate = by_id.get(dev.twin_of) if dev.twin_of else None
            if mate is not None:
                used += int(mate.traffic_rx_month) + int(mate.traffic_tx_month)
            if used >= int(dev.traffic_limit):
                for d in _pair(db, dev.id):
                    db.device_mask_apply(d, or_bits=int(DeviceBlock.TRAFFIC_USER))
                db.add_traffic_notified(client.id, f"dev_over:{dev.id}")
                n += 1
        climit = int(client.traffic_limit or 0)
        total = sum(int(d.traffic_rx_month) + int(d.traffic_tx_month) for d in devices)
        if climit and total >= climit + int(client.bonus_bytes or 0):
            db.client_mask_apply(client.id, or_bits=int(ClientBlock.TRAFFIC_CLIENT))
            for dev in devices:
                db.device_mask_apply(dev.id, or_bits=int(DeviceBlock.TRAFFIC_CLIENT))
                n += 1
            db.add_traffic_notified(client.id, "cli_over")
    return n


def recheck(db_path: str) -> dict:
    """Пересчёт на собранной базе. {"expired": n, "traffic": n}."""
    from awgbot.infra.db import Database
    from awgbot.domain.services import Services
    db = Database(db_path)
    try:
        before = {c.id for c in db.list_clients(include_service=False) if c.status == "expired"}
        with _no_iptables():
            Services(db).check_expiry()            # уведомления выбрасываются: уже уходили
        after = {c.id for c in db.list_clients(include_service=False) if c.status == "expired"}
        blocked = traffic(db)
        return {"expired": len(after - before), "traffic": blocked}
    finally:
        db.close()
