"""notices.py — часть пакета admin (см. __init__): уведомления пользователю о правках
администратора и админу — имена профилей ссылками."""

from __future__ import annotations

from awgbot.util import timeutil
from ..fmt import _e, gb, profile_link, _n_devices, plural_ru


# ─────────────────────────────────────────────────────────────────────────────
# Уведомления (тексты) — имена профилей ссылками
# ─────────────────────────────────────────────────────────────────────────────

def _slots_phrase(count: int, limit: int) -> str:
    """«Теперь у тебя 1 из 1 устройства» — родительный падеж от лимита."""
    if not limit:
        return f"Теперь у тебя {_n_devices(count)}"
    word = plural_ru(limit, "устройства", "устройств", "устройств")
    return f"Теперь у тебя {count} из {limit} {word}"


def reassign_donor_notice(name: str, count: int, limit: int) -> str:
    return f"Администратор перенёс устройство «{_e(name)}» из твоего профиля в другой.\n" + _slots_phrase(count, limit)


def reassign_recipient_notice(name: str, count: int, limit: int, *,
                              recipient_is_admin: bool = False) -> str:
    head = (f"Устройство «{_e(name)}» добавлено в твой профиль." if recipient_is_admin
            else f"Администратор добавил в твой профиль устройство «{_e(name)}».")
    return head + "\n" + _slots_phrase(count, limit)


def reassign_recipient_notice_with_slot(name: str, count: int, limit: int, *,
                                        recipient_is_admin: bool = False) -> str:
    if recipient_is_admin:
        head = f"Устройство «{_e(name)}» добавлено в твой профиль, лимит устройств увеличен."
    else:
        head = f"Администратор добавил в твой профиль устройство «{_e(name)}» и увеличил лимит устройств."
    return head + "\n" + _slots_phrase(count, limit)


def activated_admin_notice(client_or_name, who: str, bot_username: str = "") -> str:
    """«🎉 [Ксюша]: доступ активирован (@handle)»; имя строкой — без ссылки."""
    name = (profile_link(client_or_name, bot_username) if hasattr(client_or_name, "id")
            else _e(str(client_or_name)))
    return f"🎉 {name}: доступ активирован ({_e(who)})"


# ── клиенту о правке условий администратором (с тумблером «Уведомить») ──────

def traffic_limit_changed_notice(old_b: int, new_b: int) -> str:
    """«Лимит трафика изменён: ∞ → 100 ГБ в месяц»."""
    def _f(b):
        return gb(b) if b else "∞"
    tail = " ГБ в месяц" if new_b else " в месяц"
    return f"Лимит трафика изменён: {_f(old_b)} → {_f(new_b)}{tail}"


def period_changed_notice(end) -> str:
    """«Срок подписки изменён: до 15.10.27» / «… бессрочно»."""
    return "Срок подписки изменён: " + (f"до {timeutil.fmt_end_ui(end)}" if end else "подписка бессрочная")


def pause_lifted_notice(new_end) -> str:
    """«▶️ Пауза снята администратором — подписка до 15.09.27»."""
    sub = f"подписка до {timeutil.fmt_end_ui(new_end)}" if new_end else "подписка бессрочная"
    return f"▶️ Пауза снята администратором — {sub}"
