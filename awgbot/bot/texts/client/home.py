"""home.py — часть пакета client (см. __init__): главная клиента и гостя, заголовки устройств."""

from __future__ import annotations

from awgbot.bot import ui
from ..fmt import access_status_line, _e, client_link, owner_link
from .common import (
    DEV_PAYLOAD, _link, status_line, _pause_visibility, subscription_short, traffic_short,
    held_devices_tail, devices_short, limit_exhausted_line)
from .devices import _of_limit
from .subscription import subscription_status_only


# ─────────────────────────────────────────────────────────────────────────────
# Главная клиента и гостя
# ─────────────────────────────────────────────────────────────────────────────

def greeting_client(client, server_ok: bool, slots: tuple[int, int] = None,
                    routing_ok: bool = None, held=(), traffic: dict | None = None,
                    *, routing_on: bool = False, bot_username: str = "") -> str:
    """Главная клиента: имя, пустая строка, затем VPN и РФ-доступ; подписка и
    трафик; устройства. routing_ok=None — РФ-доступ профилю не выдан, его нет вовсе."""
    lines = [ui.head(f"👋 {_e(client.name)}"), "",
             status_line(server_ok, routing_ok, routing_on, bot_username)]
    paused, _, pause_visible = _pause_visibility(client)
    access = access_status_line(client)
    if access and (pause_visible or not paused):   # тихая пауза админа клиенту не видна
        lines.append(access)
    sub = subscription_short(client, bot_username)
    if traffic is not None:
        tr = traffic_short(traffic["rx_month"], traffic["tx_month"],
                           client.traffic_limit, client.bonus_bytes)
        if tr:
            sub += f" · {tr}"
    lines.append(sub)
    if slots is not None:
        lines.append(devices_short(slots, held))
    return "\n".join(lines)


def device_link(dev, bot_username: str = "") -> str:
    """Имя устройства ссылкой на его карточку (/start dev-<id>)."""
    return _link(bot_username, f"{DEV_PAYLOAD}-{dev.id}", dev.name)


def _guest_traffic_lines(held, donor, bot_username: str = "") -> list[str]:
    """«📊 Планшет: 0.8 из 100 ГБ» по каждому удерживаемому — против своего
    лимита, иначе профиля владельца; имя — ссылка на карточку; без лимитов и
    без трафика — строки нет."""
    rows = []
    for d in held:
        used = int(d.traffic_rx_month) + int(d.traffic_tx_month)
        limit = int(d.traffic_limit) or int(donor.traffic_limit)
        if not limit and not used:
            continue
        rows.append(f"📊 {device_link(d, bot_username)}: {_of_limit(used, limit)}")
    return rows


def greeting_guest(name: str, server_ok: bool, donor, held, routing_ok: bool = None,
                   *, routing_on: bool = False, bot_username: str = "") -> str:
    """Главная гостя: имя; VPN и РФ-доступ; подписка владельца (только статус —
    срок его дело); трафик по устройствам. name — имя из Telegram."""
    held = list(held)
    if donor is None or not held:
        return ui.head(f"👋 {_e(name)}", meta=["устройств нет — попроси у друга новый код"])
    lines = [ui.head(f"👋 {_e(name)}"), "",
             status_line(server_ok, routing_ok, routing_on, bot_username),
             f"💳 Подписка профиля {client_link(donor)}: {subscription_status_only(donor)}"]
    lines += _guest_traffic_lines(held, donor, bot_username)
    return "\n".join(lines)


def devices_header(used: int, limit: int, held=(), *, guest: bool = False) -> str:
    """Заголовок «📱 Устройства»: клиент — «· 2 из 3 (+1 от профиля Вася)» и
    строка «Лимит исчерпан…»; гость — «· 2 · от профиля Вася»."""
    if guest:
        n = len(list(held))
        return ui.head("📱 Устройства", meta=[str(n), f"от профиля {owner_link(held[0])}"]) if n else ui.head("📱 Устройства")
    head = ui.head("📱 Устройства", meta=[f"{used}" + (f" из {limit}" if limit else "") + held_devices_tail(held)])
    tail = limit_exhausted_line(used, limit)
    return head + (f"\n{tail}" if tail else "")


def pick_device_header(kind: str) -> str:
    """«🔗 Ссылка — для какого устройства?» по виду выдачи."""
    label = {"link": ui.head("🔗 Ссылка"), "qr": ui.head("🔳 QR"), "file": ui.head("📄 Файл")}[kind]
    return f"{label} — для какого устройства?"
