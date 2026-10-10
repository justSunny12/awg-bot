"""common.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from awgbot.domain.gwchecks import CHECK_GROUPS_QUIET_UNKNOWN
from awgbot.util import timeutil


def _gw_link_short(st) -> str:
    """«📡 Линк 🟢 40 с» — линк одной строкой панели."""
    if not st.link_up:
        return "📡 Линк 🔴 интерфейс лежит"
    if st.handshake_age is None:
        return "📡 Линк 🟡 хендшейка не было"
    from awgbot.core import settings as s
    if st.handshake_age <= s.get_int("app.gateway.handshake_max_age", 300):
        return f"📡 Линк 🟢 {timeutil.age_short(st.handshake_age)}"
    return f"📡 Линк 🟡 {timeutil.age_short(st.handshake_age)}"


def _uniq(names) -> list[str]:
    """Имена проверок без повторов: «применение локальной сети» бывает у двух."""
    return list(dict.fromkeys(names))


def _gw_health_summary(checks) -> str:
    broken = [c for c in checks if c.ok is False]
    # «не проверено» у SMB соседних сетей (нет avahi на шлюзе без NAS) и своих
    # списков (ждут синхронизации) — штатно, в сводку панели не идёт
    unknown = [c for c in checks if c.ok is None and getattr(c, "group", "") not in CHECK_GROUPS_QUIET_UNKNOWN]
    if broken:
        return f"🔴 проблем: {len(broken)} — " + ", ".join(c.name for c in broken[:4])
    if unknown:
        return "⚪ не проверено: " + ", ".join(c.name for c in unknown[:4])
    return "✅"

