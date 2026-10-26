"""devices.py — часть пакета client (см. __init__): карточки устройств, удаление, блокировка, передача, добавление."""

from __future__ import annotations

from awgbot.util import timeutil
from awgbot.bot import ui
from awgbot.core.enums import FriendStatus
from ..fmt import (
    device_state, _e, used_of_limit, gb, gb_str, volume, holder_link, owner_link, owner_name)
from .common import TELEGRAM_RISK


# ─────────────────────────────────────────────────────────────────────────────
# Карточки устройств у пользователя и гостя
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


def _blocked_line(dev, *, user_as: str = "") -> str:
    """«⛔ Заблокировано: тобой», «⛔ Заблокировано: подписка истекла».
    user_as — кем подписать блок своей кнопкой (бит USER) глазами зрителя:
    на своём и на переданном у держателя — «тобой», у владельца переданного —
    «профилем [Артём]»; пусто — подпись причины как есть («владельцем»)."""
    from awgbot.core import blocks
    mask = int(dev.block_reason)
    reasons = blocks.device_reasons_ru(mask, for_admin=False)
    if user_as and blocks.has(mask, blocks.DeviceBlock.USER):
        own = blocks.device_reasons_ru(int(blocks.DeviceBlock.USER), for_admin=False)[0]
        reasons = [user_as if r == own else r for r in reasons]
    return "⛔ Заблокировано: " + ", ".join(reasons) if reasons else ""


def device_card_own(dev, profile_limit_bytes: int) -> str:
    """Карточка своего устройства: «🟢 iPhone» / «Был в сети 2 мин назад ·
    📊 3.2 из 50 ГБ (лимит устройства)» + блокировка, приглашение, пометка
    «добавлено не ботом». Адрес устройства — только у админа."""
    parts = [ui.head(f"{device_state(dev)} {_e(dev.name)}"),
             f"{_seen(dev)} · {_usage(dev, profile_limit_bytes, 'лимит профиля')}"]
    blocked = _blocked_line(dev, user_as="тобой")      # свой блок своей кнопкой — «тобой», не «владельцем»
    if blocked:
        parts.append(blocked)
    if dev.friend_status == FriendStatus.PENDING:
        parts.append("⏳ приглашение другу ждёт активации")
    if not dev.is_managed:
        parts.append(UNMANAGED_DEVICE_LINE)
    return "\n".join(parts)


def device_card_lent(dev, profile_limit_bytes: int) -> str:
    """Своё переданное — у владельца: кто управляет, чей лимит; блок своей
    кнопкой здесь ставит только держатель — так и подписан."""
    parts = [ui.head(f"{device_state(dev)} {_e(dev.name)}", meta=[f"управляется профилем {holder_link(dev)}"]),
             f"{_seen(dev)} · {_usage(dev, profile_limit_bytes, 'лимит профиля')}"]
    blocked = _blocked_line(dev, user_as=f"профилем {holder_link(dev)}")
    if blocked:
        parts.append(blocked)
    return "\n".join(parts)


def device_card_held(dev, owner_limit_bytes: int) -> str:
    """Удерживаемое (от друга) — у держателя: от кого, чей лимит; свой блок —
    «тобой» (владелец у устройства — друг, и «владельцем» читалось бы как о нём)."""
    parts = [ui.head(f"{device_state(dev)} {_e(dev.name)}", meta=[f"от профиля {owner_link(dev)}"]),
             f"{_seen(dev)} · {_usage(dev, owner_limit_bytes, f'лимит профиля {_e(owner_name(dev))}')}"]
    blocked = _blocked_line(dev, user_as="тобой")
    if blocked:
        parts.append(blocked)
    return "\n".join(parts)


UNMANAGED_DEVICE_LINE = "✳️ Добавлено не ботом — ссылки нет: удали и добавь заново"


def unmanaged_device_dialog(name: str) -> str:
    """Диалог «удали» на выдаче устройству без ключа с главной — с именем:
    карточки на экране нет, без имени непонятно, о чём речь."""
    return f"✳️ Устройство «{_e(name)}» добавлено не ботом — ссылки нет: удали и добавь заново"


# ── удаление, блокировка, передача ───────────────────────────────────────────

def device_delete_ask(dev, *, only: bool = False, lent: bool = False, held: bool = False) -> str:
    """Вопрос удаления: своё / единственное / переданное (у владельца) /
    удерживаемое (у держателя; only — единственное у гостя: следующее — только
    по новому приглашению, и тот же риск для Telegram)."""
    name = _e(dev.name)
    if held:
        if only:
            return (f"⚠️ Удалить устройство «{name}»? Оно у тебя единственное — "
                    f"следующее получишь только по новому приглашению от профиля {owner_link(dev)}\n"
                    f"{TELEGRAM_RISK}")
        return (f"🗑 Удалить устройство «{name}»?\n"
                f"Вернуть его можно будет только по новому приглашению от профиля {owner_link(dev)}")
    if lent:
        return (f"🗑 Удалить устройство «{name}»?\n"
                f"У профиля {holder_link(dev)} пропадёт доступ; новое устройство он получит "
                "только с новым приглашением от тебя")
    if only:
        return (f"⚠️ Удалить устройство «{name}»? Оно у тебя единственное\n"
                f"{TELEGRAM_RISK}, и добавить новое будет нечем")
    return (f"🗑 Удалить устройство «{name}»?\n"
            "VPN на нём перестанет работать. Добавишь снова — понадобится новая ссылка")


def device_removed(name: str) -> str:
    """«🗑 Устройство «Ноут» удалено» — итог держателя первой строкой его экрана."""
    return f"🗑 Устройство «{_e(name)}» удалено"


def device_deleted(name: str, used: int, limit: int) -> str:
    """«🗑 Устройство «MacBook» удалено · можно добавить ещё 2» — первой строкой экрана."""
    head = device_removed(name)
    free = limit - used
    if limit == 0 or free <= 0:
        return head
    return f"{head} · можно добавить ещё {free}"


def block_device_ask(name: str, *, owner: str = "") -> str:
    """Подтверждение блокировки своей кнопкой. owner — ссылка на владельца
    переданного устройства (у держателя): он получит уведомление."""
    lines = [f"🔒 Заблокировать устройство «{_e(name)}»?",
             "VPN на нём перестанет работать, пока не разблокируешь"
             + (f". Профиль {owner} получит уведомление" if owner else ""),
             f"{TELEGRAM_RISK}, и разблокировать будет нечем"]
    return "\n".join(lines)


def transfer_ask(name: str) -> str:
    n = _e(name)
    return (f"👤 Передать устройство «{n}» другу?\n"
            "Друг получит приглашение и подключит это устройство у себя; у тебя VPN "
            "по нему работать перестанет — заведи себе новое. "
            "Устройство останется в твоём лимите")


# ── добавление устройства ────────────────────────────────────────────────────

def add_device_prompt(used: int, limit: int, *, for_friend: bool) -> str:
    slots = f" · {used} из {limit}" if limit else ""
    if for_friend:
        return (f"{ui.head('👤 Устройство для друга')}{slots}{' · займёт твой слот' if limit else ''}\n\n"
                "Как назвать? Имя увидит друг")
    return f"{ui.head('➕ Новое устройство')}{slots}\n\nКак назвать? Например: «iPhone»"


def device_created(name: str, profile_limit_bytes: int) -> str:
    """«✅ Устройство «iPhone» создано · трафик в пределах 100 ГБ профиля»."""
    head = f"✅ Устройство «{_e(name)}» создано"
    if profile_limit_bytes:
        return f"{head} · трафик в пределах {gb_str(profile_limit_bytes)} профиля"
    return head


def device_limit_prompt(name: str, profile_limit_bytes: int, current_bytes: int | None = None) -> str:
    """«📊 Лимит трафика устройства «Телефон» · сейчас 10 ГБ · не больше 100 ГБ
    профиля» (обе роли, канон «устройство «X»»); своего лимита нет — «сейчас как
    у профиля · не больше 100 ГБ» (словами кнопки «∞ Как у профиля»), у профиля
    без лимита — «сейчас ∞»; current_bytes=None — устройство ещё создаётся,
    текущего значения нет."""
    head = f"{ui.head('📊 Лимит трафика')} устройства «{_e(name)}»"
    as_profile = current_bytes == 0 and bool(profile_limit_bytes)
    if current_bytes is not None:
        now = gb_str(current_bytes) if current_bytes else ("как у профиля" if as_profile else "∞")
        head += f" · сейчас {now}"
    if profile_limit_bytes:
        return f"{head} · не больше {gb_str(profile_limit_bytes)}{'' if as_profile else ' профиля'}"
    return head


def device_limit_other_prompt(profile_limit_bytes: int, name: str = "",
                              current_bytes: int | None = None) -> str:
    """Приглашение «✏️ Другое»: заголовок, пустая строка, подсказка. name и
    current_bytes — у админа: «✏️ Лимит трафика устройства «Телефон» · сейчас ∞»."""
    head = ui.head('✏️ Лимит трафика')
    if name:
        head += f" устройства «{_e(name)}»"
    if current_bytes is not None:
        head += f" · сейчас {gb_str(current_bytes) if current_bytes else '∞'}"
    if profile_limit_bytes:
        return (f"{head} · не больше {gb(profile_limit_bytes)} ГБ (лимит профиля)\n\n"
                "Пришли число ГБ; 0 — как у профиля")
    return f"{head}\n\nПришли число ГБ; 0 — без лимита"


def device_limit_over(profile_limit_bytes: int) -> str:
    return f"⚠️ Не больше {gb_str(profile_limit_bytes)} — лимита профиля"


NUMBER_BAD = "⚠️ Нужно целое число ГБ"
NAME_EMPTY = "⚠️ Имя пустое — пришли ещё раз"


def limit_note(old_bytes: int, new_bytes: int, profile_limit_bytes: int = 0) -> str:
    """Итог правки лимита первой строкой карточки: «✅ Лимит трафика: ∞ → 50 ГБ»;
    снят у профиля с лимитом — «✅ Лимит трафика: 50 ГБ → ∞ (не более 100 ГБ —
    лимит профиля)»."""
    def _f(b):
        return gb_str(b) if b else "∞"
    tail = (f" (не более {gb_str(profile_limit_bytes)} — лимит профиля)"
            if not new_bytes and profile_limit_bytes else "")
    return f"✅ Лимит трафика: {_f(old_bytes)} → {_f(new_bytes)}{tail}"


def name_note(old: str, new: str) -> str:
    return f"✅ Имя устройства: {_e(old)} → {_e(new)}"


def device_name_prompt(name: str) -> str:
    """«✏️ Имя устройства · сейчас «Телефон»», пустая строка, подсказка."""
    return f"{ui.head('✏️ Имя устройства')} · сейчас «{_e(name)}»\n\nПришли новое имя"
