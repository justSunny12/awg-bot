"""reserve.py — часть пакета notices (см. __init__): уведомления пары серверов
из доменного слоя — связь с резервом (Т-РУ8), совместимость направлений
(Т-РУ9), смена админа второго сервера, удаление пары второй стороной. Имена
серверов — «имя», без точки в конце; уведомления «один раз на расхождение»."""
from __future__ import annotations

import html

from awgbot.util import timeutil


def _e(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _n(name: str) -> str:
    return f"«{_e(name)}»"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except (ValueError, TypeError):
        return ""


def peer_down(peer: str, since: str = "") -> str:
    """Т-РУ8: линк пары молчит."""
    at = _when(since)
    return f"🔴 Нет связи с резервом {_n(peer)}" + (f" с {at}" if at else "") + " — пока её нет, клиенты к нему не переедут"


def peer_up(peer: str) -> str:
    return f"🟢 Связь с резервом {_n(peer)} восстановлена"


def peer_unpaired(peer: str) -> str:
    return f"ℹ️ {_n(peer)} удалил пару: резерва больше нет, его данные с этого сервера сняты"


def direction_off(peer: str, why: str) -> str:
    """Т-РУ9: направление выключилось (версия, возможности)."""
    return f"⚠️ Резерв {_n(peer)}: {_e(why)}"


def direction_on(peer: str) -> str:
    return f"🟢 Резерв {_n(peer)} снова совместим"


def peer_admin_changed(peer: str) -> str:
    return f"ℹ️ У {_n(peer)} сменился админ — его прежний аккаунт потерял доступ к экрану резерва в этом боте"
