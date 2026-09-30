"""slots.py — слоты шлюзов: таблица и бит метки слота, политика слота."""

from __future__ import annotations


from awgbot.core import config

from . import base

# ── слоты шлюзов ──────────────────────────────────
# У каждого слота своя таблица и свой бит метки — только ради зонда резерва:
# клиентский трафик ходит по таблице фичи, куда указывает активный линк.

def slot_table(slot_id: int) -> int:
    return config.ROUTING_TABLE + int(slot_id)


def slot_mark(slot_id: int) -> int:
    return 1 << int(slot_id)


def _slot_rule(slot_id: int) -> list[str]:
    m = f"0x{slot_mark(slot_id):x}"
    return ["fwmark", f"{m}/{m}", "lookup", str(slot_table(slot_id))]


def ensure_slot_policy(slot_id: int, iface: str) -> None:
    """Таблица и правило слота: путь зонда резерва через ЕГО линк. Идемпотентно."""
    base._host(["ip", "route", "replace", "default", "dev", iface, "table", str(slot_table(slot_id))])
    rules = base._host(["ip", "rule", "show"], check=False, timeout=base._PROBE_TIMEOUT)
    text = rules.stdout.decode(errors="replace")
    if f"lookup {slot_table(slot_id)}" not in text:
        base._host(["ip", "rule", "add", *_slot_rule(slot_id)])


def drop_slot_policy(slot_id: int, iface: str = "") -> None:
    """Снять таблицу и правило слота (слот убран). Отсутствие — не ошибка."""
    while base._host_ok(["ip", "rule", "del", *_slot_rule(slot_id)]):
        pass
    base._host(["ip", "route", "flush", "table", str(slot_table(slot_id))], check=False)
