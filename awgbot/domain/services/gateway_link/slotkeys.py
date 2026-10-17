"""slotkeys.py — реестр слотовых ключей server_state: что из состояния слота
снимается при снятии слота, а что — уже при замене устройства в нём.

Раньше ключи слота перечисляли три ручных списка (снятие слота, забыть канал,
забыть документ конфигурации), и новый ключ, забытый в одном из них,
переживал снятие: новый слот с тем же номером наследовал хвосты прежнего.
Теперь каждый слотовый ключ записан здесь один раз с пометкой, когда он
снимается; сторож-тест сверяет реестр с кодом пакетов gwchannel/,
gateway_link/ и routing/.

Имя ключа — основа без номера: в базе он лежит как «<имя>_<слот>»; имя с «{}»
— шаблон, номер слота встаёт на его место («routing_gw_{}_up_streak»).
"""
from __future__ import annotations

from typing import NamedTuple

SLOT = "slot"        # снимается только со слотом: свойство слота, а не устройства в нём
DEVICE = "device"    # снимается и при замене устройства: описывает стоящее на шлюзе


class SlotKey(NamedTuple):
    when: str        # SLOT | DEVICE
    part: str        # чья часть: cfg — документ, link — канал, rekey — смена ключей, slot, keep


KEYS: dict[str, SlotKey] = {
    # документ конфигурации слота (gwchannel/cfg.py)
    "gwcfg_gen": SlotKey(DEVICE, "cfg"),
    "gwcfg_hash": SlotKey(DEVICE, "cfg"),
    "gwcfg_ack": SlotKey(DEVICE, "cfg"),
    "gwcfg_link_err": SlotKey(DEVICE, "cfg"),
    "gwcfg_uplink_err": SlotKey(DEVICE, "cfg"),
    "gwcfg_env_err": SlotKey(DEVICE, "cfg"),
    "gwcfg_uplink_declined_told": SlotKey(DEVICE, "cfg"),
    "gwcfg_token_failed_told": SlotKey(DEVICE, "cfg"),
    # канал линка: сессия, снимок, ответы (gwchannel/)
    "gwlink_session": SlotKey(DEVICE, "link"),
    "gwlink_seen": SlotKey(DEVICE, "link"),
    "gwlink_snap": SlotKey(DEVICE, "link"),
    "gwlink_snap_rev": SlotKey(DEVICE, "link"),
    "gwlink_snap_at": SlotKey(DEVICE, "link"),
    "gwlink_snap_ts": SlotKey(DEVICE, "link"),
    "gwlink_error": SlotKey(DEVICE, "link"),
    "gwlink_caps": SlotKey(DEVICE, "link"),
    "gwlink_lists": SlotKey(DEVICE, "link"),
    "gwlink_svc": SlotKey(DEVICE, "link"),
    "gwlink_peer_svc": SlotKey(DEVICE, "link"),
    "gwlink_bundle_msg": SlotKey(DEVICE, "link"),
    "gwlink_own_upto": SlotKey(DEVICE, "link"),
    "gwlink_own_ack": SlotKey(DEVICE, "link"),
    "gwlink_own_rej": SlotKey(DEVICE, "link"),
    "gwlink_own_cap": SlotKey(DEVICE, "link"),
    # подтверждение настроек и итог применения файла — доставки, снятой в
    # 3.3.1: код их больше не пишет, у слотов, заведённых раньше, они ещё лежат
    "gwlink_ack": SlotKey(DEVICE, "link"),
    "gwlink_applied": SlotKey(DEVICE, "link"),
    # смена ключей линка каналом (gateway_link/rekey.py): замена устройства
    # снимает переход своим путём (gateway_rekey_abort), со слотом — ключи
    "gwlink_rekey": SlotKey(SLOT, "rekey"),
    "gwlink_rekey_cancel": SlotKey(SLOT, "rekey"),
    # слот: файл первого применения, расхождение, бот, токен, порт, живость
    "gw_bundle_issued_at": SlotKey(SLOT, "slot"),
    "gw_bundle_ssh_allow": SlotKey(SLOT, "slot"),
    "gw_drift_notified": SlotKey(SLOT, "slot"),
    "gw_bot_me": SlotKey(SLOT, "slot"),
    "gw_token_push": SlotKey(SLOT, "slot"),
    "gw_link_port_hand": SlotKey(SLOT, "slot"),
    "routing_gw_ping": SlotKey(SLOT, "slot"),
    "routing_gw_{}_up_streak": SlotKey(SLOT, "slot"),
    "routing_gw_{}_announced": SlotKey(SLOT, "slot"),
    "routing_gw_{}_down_since": SlotKey(SLOT, "slot"),
    # копия настроек шлюза: нужна новому устройству слота — переживает замену
    "gwkeep": SlotKey(SLOT, "keep"),
}


def key(name: str, slot_id: int) -> str:
    """Ключ server_state слота: «<имя>_<слот>» или шаблон с номером внутри."""
    return name.format(int(slot_id)) if "{}" in name else f"{name}_{int(slot_id)}"


def names(*, when: str | None = None, part: str | None = None) -> tuple[str, ...]:
    """Имена реестра по пометке и части; без условий — все."""
    return tuple(n for n, k in KEYS.items()
                 if (when is None or k.when == when) and (part is None or k.part == part))


def forget(db, slot_id: int, which) -> None:
    """Снять ключи слота по списку имён реестра."""
    for name in which:
        db.set_state(key(name, slot_id), "")
