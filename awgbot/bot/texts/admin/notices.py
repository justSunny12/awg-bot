"""notices.py — часть пакета admin (см. __init__): уведомления, имена профилей ссылками."""

from __future__ import annotations

from ..fmt import _e, profile_link, _n_devices


# ─────────────────────────────────────────────────────────────────────────────
# Уведомления (тексты) — имена профилей ссылками
# ─────────────────────────────────────────────────────────────────────────────

def _slots_phrase(count: int, limit: int) -> str:
    return f"Теперь у тебя {count} из {limit} устройств" if limit else f"Теперь у тебя {_n_devices(count)}"


def reassign_donor_notice(name: str, count: int, limit: int) -> str:
    return f"Устройство «{_e(name)}» удалено из твоего профиля администратором.\n" + _slots_phrase(count, limit)


def reassign_recipient_notice(name: str, count: int, limit: int, *,
                              recipient_is_admin: bool = False) -> str:
    tail = "" if recipient_is_admin else " администратором"
    return f"Устройство «{_e(name)}» добавлено в твой профиль{tail}.\n" + _slots_phrase(count, limit)


def reassign_recipient_notice_with_slot(name: str, count: int, limit: int, *,
                                        recipient_is_admin: bool = False) -> str:
    tail = "" if recipient_is_admin else " администратором"
    return (f"Устройство «{_e(name)}» добавлено в твой профиль{tail}, максимальное количество устройств увеличено.\n"
            + _slots_phrase(count, limit))


def activated_admin_notice(client_or_name, who: str, bot_username: str = "") -> str:
    """«🎉 [Ксюша]: доступ активирован (@handle)»; имя строкой — без ссылки."""
    name = (profile_link(client_or_name, bot_username) if hasattr(client_or_name, "id")
            else _e(str(client_or_name)))
    return f"🎉 {name}: доступ активирован ({_e(who)})"
