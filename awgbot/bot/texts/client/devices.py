"""devices.py — часть пакета client (см. __init__): карточки устройств, удаление, блокировка, передача, добавление."""

from __future__ import annotations

from awgbot.util import timeutil
from awgbot.bot import ui
from awgbot.core.enums import FriendStatus
from ..fmt import (
    device_state, _e, used_of_limit, gb, gb_str, volume, holder_link, owner_link, owner_name)
from .common import TELEGRAM_RISK


# ─────────────────────────────────────────────────────────────────────────────
# Карточки устройств у клиента и гостя
# ─────────────────────────────────────────────────────────────────────────────

def _of_limit(used: int, limit_bytes: int, note: str = "") -> str:
    """«3.2 из 50 ГБ (лимит устройства)»; без лимита — «3.2 ГБ (безлимит)»."""
    if not limit_bytes:
        return f"{volume(used)} (безлимит)"
    return used_of_limit(used, limit_bytes, note)


def _usage(dev, profile_limit_bytes: int, whose: str) -> str:
    """«📊 3.2 из 50 ГБ (лимит устройства)» / «📊 3.2 из 100 ГБ (лимит
    профиля)» / «📊 3.2 ГБ (безлимит)»."""
    used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
    if dev.traffic_limit:
        return "📊 " + used_of_limit(used, dev.traffic_limit, "лимит устройства")
    return "📊 " + _of_limit(used, profile_limit_bytes, whose)


def _seen(dev) -> str:
    ago = timeutil.fmt_ago(dev.last_handshake)
    return "Не подключался" if ago == "никогда" else f"Был в сети {ago}"


def _blocked_line(dev) -> str:
    """«⛔ Заблокировано: владельцем», «⛔ Заблокировано: подписка истекла»."""
    from awgbot.core import blocks
    reasons = blocks.device_reasons_ru(int(dev.block_reason), for_admin=False)
    return "⛔ Заблокировано: " + ", ".join(reasons) if reasons else ""


def device_card_own(dev, profile_limit_bytes: int) -> str:
    """Карточка своего устройства: «🟢 iPhone» / «Был в сети 2 мин назад ·
    📊 3.2 из 50 ГБ (лимит устройства)» + блокировка, приглашение, пометка
    «добавлено не ботом». Адрес устройства — только у админа."""
    parts = [ui.head(f"{device_state(dev)} {_e(dev.name)}"),
             f"{_seen(dev)} · {_usage(dev, profile_limit_bytes, 'лимит профиля')}"]
    blocked = _blocked_line(dev)
    if blocked:
        parts.append(blocked)
    if dev.friend_status == FriendStatus.PENDING:
        parts.append("⏳ приглашение другу ждёт активации")
    if not dev.is_managed:
        parts.append(UNMANAGED_DEVICE_LINE)
    return "\n".join(parts)


def device_card_lent(dev, profile_limit_bytes: int) -> str:
    """Своё переданное — у владельца: кто управляет, чей лимит."""
    parts = [ui.head(f"{device_state(dev)} {_e(dev.name)}", meta=[f"управляется профилем {holder_link(dev)}"]),
             f"{_seen(dev)} · {_usage(dev, profile_limit_bytes, 'лимит твоего профиля')}"]
    blocked = _blocked_line(dev)
    if blocked:
        parts.append(blocked)
    return "\n".join(parts)


def device_card_held(dev, owner_limit_bytes: int) -> str:
    """Удерживаемое (от друга) — у держателя: от кого, чей лимит."""
    parts = [ui.head(f"{device_state(dev)} {_e(dev.name)}", meta=[f"от профиля {owner_link(dev)}"]),
             f"{_seen(dev)} · {_usage(dev, owner_limit_bytes, f'лимит профиля {_e(owner_name(dev))}')}"]
    blocked = _blocked_line(dev)
    if blocked:
        parts.append(blocked)
    return "\n".join(parts)


UNMANAGED_DEVICE_LINE = "✳️ Добавлено не ботом — ссылки нет: удали и добавь заново"


# ── удаление, блокировка, передача ───────────────────────────────────────────

def device_delete_ask(dev, *, only: bool = False, lent: bool = False, held: bool = False) -> str:
    """Вопрос удаления: своё / единственное / переданное (у владельца) /
    удерживаемое (у держателя)."""
    name = _e(dev.name)
    if held:
        return (f"🗑 Удалить {name}?\n"
                "Новое устройство можно будет создать только по коду от друга")
    if lent:
        return (f"🗑 Удалить {name}?\n"
                f"У профиля {holder_link(dev)} пропадёт доступ; новое устройство он получит "
                "только с новым приглашением от тебя")
    if only:
        return (f"⚠️ Удалить {name} — единственное устройство?\n"
                "VPN выключится сразу. " + TELEGRAM_RISK)
    return (f"🗑 Удалить {name}?\n"
            "Ссылка перестанет работать; решишь добавить устройство снова — ссылка изменится")


def device_removed(name: str) -> str:
    """«🗑 Ноут удалено» — итог держателя первой строкой его экрана."""
    return f"🗑 {_e(name)} удалено"


def device_deleted(name: str, used: int, limit: int) -> str:
    """«🗑 MacBook удалено · можно добавить ещё 2» — первой строкой экрана."""
    head = f"🗑 {_e(name)} удалено"
    free = limit - used
    if limit == 0 or free <= 0:
        return head
    return f"{head} · можно добавить ещё {free}"


def block_device_ask(name: str) -> str:
    return (f"🛑 Заблокировать {_e(name)}?\n"
            "Перестанет подключаться, пока не разблокируешь. " + TELEGRAM_RISK)


def transfer_ask(name: str) -> str:
    n = _e(name)
    return (f"👤 Передать {n} другу?\n"
            "Друг получит это подключение; одно подключение на двух устройствах "
            "работать не будет\n"
            f"Если устройство «{n}» твоё — сначала заведи себе новое")


# ── добавление устройства ────────────────────────────────────────────────────

def add_device_prompt(used: int, limit: int, *, for_friend: bool) -> str:
    slots = f" · {used} из {limit}" if limit else ""
    if for_friend:
        return (f"{ui.head('👤 Устройство для друга')}{slots}{' · займёт твой слот.' if limit else ''}\n"
                "Как назвать? Имя увидит друг")
    return f"{ui.head('➕ Новое устройство')}{slots}\nКак назвать? Например: «iPhone»"


def device_created(name: str, profile_limit_bytes: int) -> str:
    """«✅ iPhone создано · трафик в пределах 100 ГБ профиля»."""
    head = f"✅ {_e(name)} создано"
    if profile_limit_bytes:
        return f"{head} · трафик в пределах {gb_str(profile_limit_bytes)} профиля"
    return head


def device_limit_prompt(name: str, profile_limit_bytes: int) -> str:
    head = ui.head(f"📊 Лимит трафика устройства «{_e(name)}»")
    if profile_limit_bytes:
        return f"{head} · не больше {gb_str(profile_limit_bytes)} профиля"
    return head


def device_limit_other_prompt(profile_limit_bytes: int) -> str:
    if profile_limit_bytes:
        return f"✏️ Число ГБ, не больше {gb(profile_limit_bytes)} (лимит профиля); 0 — без ограничений в рамках лимита"
    return "✏️ Число ГБ, 0 — без лимита"


def device_limit_over(profile_limit_bytes: int) -> str:
    return f"⚠️ Не больше {gb_str(profile_limit_bytes)} — лимита профиля"


NUMBER_BAD = "⚠️ Нужно целое число ГБ"
NAME_EMPTY = "⚠️ Имя пустое — пришли ещё раз"


def limit_note(old_bytes: int, new_bytes: int, profile_limit_bytes: int = 0) -> str:
    """Итог правки лимита первой строкой карточки: «✅ Лимит: ∞ → 50 ГБ»;
    снят у профиля с лимитом — «✅ Лимит: 50 ГБ → ∞ (не более 100 ГБ — лимит
    профиля)»."""
    def _f(b):
        return gb_str(b) if b else "∞"
    tail = (f" (не более {gb_str(profile_limit_bytes)} — лимит профиля)"
            if not new_bytes and profile_limit_bytes else "")
    return f"✅ Лимит: {_f(old_bytes)} → {_f(new_bytes)}{tail}"


def name_note(old: str, new: str) -> str:
    return f"✅ Имя устройства: {_e(old)} → {_e(new)}"


def device_name_prompt(name: str) -> str:
    return ui.head(f"✏️ Новое имя для устройства «{_e(name)}»")
