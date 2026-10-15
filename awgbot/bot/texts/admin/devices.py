"""devices.py — часть пакета admin (см. __init__): мои устройства, карточка устройства, перенос и блокировка."""

from __future__ import annotations

from awgbot.util import timeutil
from awgbot.bot import ui
from ..fmt import (
    _e, human_bytes, gb, device_state, plain_ip, rf_line, updown_brief, profile_link,
    holder_link)
from .common import LIMIT_REACHED


# ─────────────────────────────────────────────────────────────────────────────
# Мои устройства и карточка устройства (админ)
# ─────────────────────────────────────────────────────────────────────────────

def my_devices_header(n: int, limit: int) -> str:
    """«📱 <b>Мои устройства</b> · 2, без лимита» / «· 2 из 5»; ни одного и без
    лимита — заголовок и строка «Своих устройств пока нет»."""
    if not n and not limit:
        return ui.head("📱 Мои устройства") + "\n\nСвоих устройств пока нет"
    tail = f" из {limit}" if limit else ", без лимита"
    return ui.head("📱 Мои устройства", meta=[f"{n}{tail}"])


def _admin_usage(dev, profile_limit_bytes: int) -> str:
    """«3.2 из 50 ГБ (↑0.4 ↓2.8)» / «3.2 из 100 ГБ (↑0.4 ↓2.8), лимит профиля» /
    «3.2 ГБ (↑0.4 ↓2.8)»; ноль — «0 из 50 ГБ» / «0 ГБ»."""
    rx, tx = int(dev.traffic_rx_month), int(dev.traffic_tx_month)
    limit = int(dev.traffic_limit) or int(profile_limit_bytes or 0)
    whose = "" if dev.traffic_limit else (", лимит профиля" if profile_limit_bytes else "")
    arrows = f" {updown_brief(rx, tx)}" if rx + tx else ""
    if limit:
        return f"{gb(rx + tx)} из {gb(limit)} ГБ{arrows}{whose}"
    return f"{human_bytes(rx + tx)}{arrows}"


def client_devices_header(client, used: int, limit: int) -> str:
    """«📱 <b>Устройства профиля Ксюша</b> · 1 из 3» — список устройств
    профиля у админа, когда в карточку они не влезли."""
    return ui.head(f"📱 Устройства профиля {_e(client.name)}", meta=[f"{used}" + (f" из {limit}" if limit else "")])


def admin_device_card(dev, client, *, rf=None, profile_limit_bytes: int = 0,
                      bot_username: str = "") -> str:
    """Карточка устройства у админа: «🟢 iPhone · 10.8.1.5 · [Ксюша]» / «В
    сети · 3.2 ГБ (↑0.4 ↓2.8) из 50» / РФ-ветка / блокировка / передача /
    «добавлено не ботом»."""
    from awgbot.core import blocks
    who = profile_link(client, bot_username) if client is not None and not getattr(client, "is_service", 0) else "без профиля"
    if timeutil.handshake_is_online(dev.last_handshake):
        seen = "В сети"                                # кружок в шапке уже зелёный
    else:
        ago = timeutil.fmt_ago(dev.last_handshake)
        seen = "Не подключался" if ago == "никогда" else f"Был в сети {ago}"
    parts = [ui.head(f"{device_state(dev, for_admin=True)} {_e(dev.name)}", meta=[plain_ip(dev.address), who]),
             f"{seen} · 📊 {_admin_usage(dev, profile_limit_bytes)}"]
    if rf is not None:                            # ноль тоже — см. карточку профиля
        parts.append(rf_line(*rf, arrows=False))
    extra = []                                    # состояния — отдельным блоком
    reasons = blocks.device_reasons_ru(int(dev.block_reason), for_admin=True)
    if reasons:
        extra.append("⛔ Заблокировано " + ", ".join(reasons))
    if getattr(dev, "is_lent", False):
        extra.append(f"👤 Передано {holder_link(dev)}")
    elif getattr(dev, "friend_status", None) == "pending":
        extra.append("⏳ приглашение другу ждёт активации")
    if not dev.is_managed:
        extra.append("✳️ Добавлено не ботом — ссылки нет: удали и добавь заново")
    if extra:
        parts.append("")
        parts.extend(extra)
    return "\n".join(parts)


def admin_device_delete_ask(dev, client, *, only: bool, bot_username: str = "") -> str:
    name = _e(dev.name)
    owned = client is not None and not getattr(client, "is_service", 0)
    who = f" ({profile_link(client, bot_username)})" if owned else ""
    head = f"🗑 Удалить устройство «{name}»{who}?"
    if only and client is not None:
        return (f"{head}\n⚠️ Это единственное устройство профиля. "
                "VPN выключится сразу; если владелец использует Telegram только через этот "
                "VPN, до бота он не достучится")
    return f"{head}\nVPN на устройстве перестанет работать; добавишь его снова — ссылка будет новой"


def device_deleted_note(dev, client=None, bot_username: str = "") -> str:
    """«✅ Устройство «Ноутбук» удалено» — первой строкой экрана, откуда пришли
    (карточка профиля там же, профиль не повторяем)."""
    return f"✅ Устройство «{_e(dev.name)}» удалено"


def _owner_tail(client, bot_username: str = "") -> str:
    """« ([Коля])» — владелец устройства ссылкой; без профиля — пусто."""
    if client is None or getattr(client, "is_service", 0):
        return ""
    return f" ({profile_link(client, bot_username)})"


def reassign_ask(dev, client=None, bot_username: str = "") -> str:
    return f"{ui.head(f'↪️ Перенос устройства «{_e(dev.name)}»')}{_owner_tail(client, bot_username)} — в какой профиль?"


def reassign_slot_ask(client, bot_username: str = "", used: int | None = None) -> str:
    """used — занято сейчас: лимит могли понизить ниже числа устройств."""
    lim = client.device_limit
    return (f"📱 У профиля {profile_link(client, bot_username)} исчерпан лимит устройств "
            f"({lim if used is None else used} из {lim}) — добавить слот?")


def limit_reached_line(used: int, limit: int) -> str:
    """«Достигнут лимит устройств: чтобы добавить новое, удали N» — N считает
    от занятого сверх лимита."""
    n = max(1, int(used) - int(limit) + 1)
    return f"{LIMIT_REACHED}: чтобы добавить новое, удали {n}"


LIMIT_REACHED_RACE = (f"⚠️ {LIMIT_REACHED}, пока ты вводил имя: подними лимит в «✏️ Изменить» "
                      "или удали лишнее")


def reassigned_note(name: str, client, bot_username: str = "", donor=None) -> str:
    """«✅ Устройство «iPhone» перенесено: [Коля] → [Ксюша]»; прежний владелец
    не передан — «… перенесено в профиль [Ксюша]»."""
    to = profile_link(client, bot_username)
    if donor is None:
        return f"✅ Устройство «{_e(name)}» перенесено в профиль {to}"
    src = "без профиля" if getattr(donor, "is_service", 0) else profile_link(donor, bot_username)
    return f"✅ Устройство «{_e(name)}» перенесено: {src} → {to}"


def block_device_ask_admin(name: str, client=None, bot_username: str = "") -> str:
    """«🔒 Блокировка устройства «Телефон» (Ксюша)» — экран с тумблером
    уведомления и кнопкой «🔒 Заблокировать»."""
    return f"{ui.head(f'🔒 Блокировка устройства «{_e(name)}»')}{_owner_tail(client, bot_username)}"
