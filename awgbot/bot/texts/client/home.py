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


GUEST_NO_DEVICES_HINT = "Попроси у друга новое приглашение и открой ссылку из него"


def guest_access_line(donor) -> str:
    """Строка состояния доступа гостя — по подписке и блокировкам владельца:
    только когда VPN на переданных устройствах не работает, со значком по
    причине и именем профиля. Что реально режет доступ гостю: исчерпанный
    лимит трафика профиля (каскад TRAFFIC_CLIENT), пауза (каскад PAUSED,
    тихая пауза администратора не видна, как и клиенту), истёкшая подписка
    (EXPIRY), блокировка профиля администратором с уведомлением
    (ADMIN_NOTIFIED; тихая — не видна)."""
    from awgbot.core import blocks
    from awgbot.core.enums import SubStatus
    mask = int(donor.block_reason or 0)
    paused, _, pause_visible = _pause_visibility(donor)
    who = client_link(donor)
    if mask & int(blocks.ClientBlock.TRAFFIC_CLIENT):
        return f"⛔ У профиля {who} исчерпан лимит трафика — доступ приостановлен"
    if paused and pause_visible:
        return f"⏸️ Подписка профиля {who} на паузе — доступ приостановлен"
    if donor.status != SubStatus.ACTIVE:
        return f"🔴 У профиля {who} истекла подписка — доступ приостановлен"
    if mask & int(blocks.ClientBlock.ADMIN_NOTIFIED):
        return f"⛔ Профиль {who} заблокирован — доступ приостановлен"
    return ""


def greeting_guest(name: str, server_ok: bool, donor, held, routing_ok: bool = None,
                   *, routing_on: bool = False, bot_username: str = "") -> str:
    """Главная гостя: имя; сервер и РФ-доступ; под ними — почему доступа нет
    (только негативная строка, по владельцу); подписка владельца (только
    статус — срок его дело); трафик по устройствам. name — имя из Telegram."""
    held = list(held)
    if donor is None or not held:
        return ui.head(f"👋 {_e(name)}", meta=["устройств нет"]) + "\n" + GUEST_NO_DEVICES_HINT
    lines = [ui.head(f"👋 {_e(name)}"), "",
             status_line(server_ok, routing_ok, routing_on, bot_username)]
    access = guest_access_line(donor)
    if access:
        lines.append(access)
    lines.append(f"💳 Подписка профиля {client_link(donor)}: {subscription_status_only(donor)}")
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
