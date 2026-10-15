"""common.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

import re

from awgbot.domain import gwchecks
from awgbot.domain.notices.gateway import mail_why, MAIL_FIX_TAIL   # общие с уведомлением домена
from awgbot.util import timeutil


def _gw_link_short(st) -> str:
    """«📡 Линк 🟢 40 с» — линк одной строкой панели; хендшейк старше порога —
    «🟡 хендшейк 12 мин назад»."""
    if not st.link_up:
        return "📡 Линк 🔴 интерфейс лежит"
    if st.handshake_age is None:
        return "📡 Линк 🟡 хендшейка не было"
    from awgbot.core import settings as s
    if st.handshake_age <= s.get_int("app.gateway.handshake_max_age", 300):
        return f"📡 Линк 🟢 {timeutil.age_short(st.handshake_age)}"
    return f"📡 Линк 🟡 хендшейк {timeutil.age_ago(st.handshake_age)}"


def _uniq(names) -> list[str]:
    """Имена проверок без повторов: «VPN-транзит» бывает у двух."""
    return list(dict.fromkeys(names))


def _gw_health_summary(checks) -> str:
    """Сводка панели: «🔴 проблем: 3 — линк, фильтр шлюза, выход наружу» /
    «✅». Проблемы — те же строки, что красные на экране «🩺 Здоровье»
    (gwchecks.health_problems); имя — без технического в скобках."""
    broken = gwchecks.health_problems(checks)
    if broken:
        return f"🔴 проблем: {len(broken)} — " + ", ".join(_uniq(_short_name(c.name) for c in broken)[:4])
    return "✅"


def _short_name(name: str) -> str:
    """«фильтр шлюза (awg_gw_guard)» → «фильтр шлюза»: в сводке панели
    техническое имя в скобках только занимает место."""
    return re.sub(r"\s*\([^)]*\)$", "", name)



__all__ = ["_gw_link_short", "_uniq", "_gw_health_summary", "_short_name", "mail_why", "MAIL_FIX_TAIL"]
