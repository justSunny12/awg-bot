"""rows.py — часть пакета schema (см. __init__): builder'ы доменных моделей из
строк выборок и параметры линка из живого конфига интерфейса."""
from __future__ import annotations

from typing import Optional

from awgbot.core import models


def _client_from_row(row) -> Optional["models.Client"]:
    """Объединённая строка (clients + подписка/квота, LEFT JOIN grace/pause) →
    доменный Client. Ленивые процессы: grace/pause материализуются только когда в
    соответствующей таблице есть строка (LEFT JOIN вернул не-NULL). Читает по
    именам колонок — работает с результатом JOIN из getter'ов."""
    if row is None:
        return None
    keys = row.keys()
    grace = None
    if "grace_used" in keys and row["grace_used"] is not None:
        if int(row["grace_used"]) or int(row["grace_pending_cut"]):
            grace = models.GraceState(used=int(row["grace_used"]),
                                      pending_cut=int(row["grace_pending_cut"]))
    pause = None
    if "pause_used_days" in keys and row["pause_used_days"] is not None:
        balance = int(row["pause_balance_days"] or 0)
        if row["pause_active_since"]:
            pause = models.PauseState(
                active_since=row["pause_active_since"],
                reserved_days=int(row["pause_reserved_days"]),
                used_days=int(row["pause_used_days"]),
                balance_days=balance,
                mode=row["pause_mode"],
                saved_end=row["pause_saved_end"],
                resume_code=(row["resume_code"] if "resume_code" in keys else None))
        elif int(row["pause_used_days"]) or balance:
            pause = models.PauseState(used_days=int(row["pause_used_days"]), balance_days=balance)
    return models.Client(
        id=int(row["id"]),
        tg_id=row["tg_id"],
        name=row["name"],
        device_limit=int(row["device_limit"]),
        block_reason=int(row["block_reason"]),
        is_service=int(row["is_service"]),
        activation_status=row["activation_status"],
        invite_code=row["invite_code"],
        created_at=row["created_at"],
        kind=(row["kind"] if "kind" in keys else "owner"),
        tg_name=(row["tg_name"] if "tg_name" in keys else "") or "",
        tg_name_at=(row["tg_name_at"] if "tg_name_at" in keys else None),
        tg_username=(row["tg_username"] if "tg_username" in keys else "") or "",
        routing_allowed=(int(row["routing_allowed"]) if "routing_allowed" in keys else 0),
        subscription=models.Subscription(
            period_start=row["period_start"], period_end=row["period_end"],
            period_kind=row["period_kind"], status=row["status"],
            notified_thresholds=row["notified_thresholds"]),
        quota=models.TrafficQuota(
            limit=int(row["traffic_limit"]), bonus_bytes=int(row["bonus_bytes"]),
            bonus_granted_month=int(row["bonus_granted_month"]),
            traffic_notified=row["traffic_notified"]),
        grace=grace, pause=pause)


def _device_from_row(row) -> Optional["models.Device"]:
    """Строка _DEVICE_SELECT (devices + traffic, LEFT JOIN friend) → Device.
    Все выборки устройств идут через этот SELECT, поэтому колонки здесь читаются
    без оглядки на их наличие. friend материализуется только у гостевых устройств
    (строка device_friend есть ⇔ friend_status не NULL)."""
    if row is None:
        return None
    friend = None
    if row["friend_status"]:
        friend = models.Friend(code=row["friend_code"], status=row["friend_status"])
    return models.Device(
        id=int(row["id"]),
        client_id=int(row["client_id"]),
        name=row["name"],
        private_key=row["private_key"],
        public_key=row["public_key"],
        preshared_key=row["preshared_key"],
        address=row["address"],
        block_reason=int(row["block_reason"]),
        routing_on=int(row["routing_on"]),
        iface=row["iface"] or "",
        twin_of=(int(row["twin_of"]) if row["twin_of"] is not None else None),
        is_gateway=int(row["is_gateway_eff"]),
        created_at=row["created_at"],
        traffic=models.DeviceTraffic(
            limit=int(row["traffic_limit"]),
            rx_month=int(row["traffic_rx_month"]), tx_month=int(row["traffic_tx_month"]),
            rx_period=int(row["traffic_rx_period"]), tx_period=int(row["traffic_tx_period"]),
            last_handshake=row["last_handshake"], missing_count=int(row["missing_count"]),
            rf_rx_month=int(row["rf_rx_month"] or 0), rf_tx_month=int(row["rf_tx_month"] or 0)),
        friend=friend,
        holder_client_id=(int(row["holder_client_id"]) if row["holder_client_id"] is not None else None),
        holder_tg_id=row["holder_tg_id"], holder_name=row["holder_name"] or "",
        holder_tg_name=row["holder_tg_name"] or "", holder_tg_username=row["holder_tg_username"] or "",
        owner_tg_id=row["owner_tg_id"], owner_name=row["owner_name"] or "",
        owner_tg_name=row["owner_tg_name"] or "", owner_tg_username=row["owner_tg_username"] or "")


def _link_conf_params(link_if: str) -> tuple[int, str]:
    """(ListenPort, /30 линка) из живого конфига интерфейса на ВПС; конфига
    нет (тесты, свежая машина) — умолчания первого слота из config."""
    import re
    from pathlib import Path
    from awgbot.core import config
    port, cidr = config.ROUTING_LINK_PORTS[0], config.ROUTING_LINK_CIDRS[0]
    try:
        text = Path(config.AWG_DIR, f"{link_if}.conf").read_text(encoding="utf-8")
    except OSError:
        return port, cidr
    m = re.search(r"(?m)^\s*ListenPort\s*=\s*(\d+)", text)
    if m:
        port = int(m.group(1))
    m = re.search(r"(?m)^\s*Address\s*=\s*([\d.]+)/(\d+)", text)
    if m:
        import ipaddress
        try:
            cidr = str(ipaddress.ip_interface(f"{m.group(1)}/{m.group(2)}").network)
        except ValueError:
            pass
    return port, cidr


def _gateway_from_row(row) -> Optional["models.Gateway"]:
    if row is None:
        return None
    return models.Gateway(
        id=int(row["id"]), device_id=int(row["device_id"]), link_if=row["link_if"],
        link_port=int(row["link_port"]), link_cidr=row["link_cidr"],
        preferred=int(row["preferred"]),
        lan_subnets=[n for n in (row["lan_subnets"] or "").split() if n],
        label=row["label"] or "", created_at=row["created_at"] or "",
        lan_mode=int(row["lan_mode"] or 0) if "lan_mode" in row.keys() else 0)
