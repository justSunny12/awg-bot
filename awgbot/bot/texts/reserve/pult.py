"""pult.py — «пульт у основы» (domain/services/reserve/pult): ответ пользователям,
пока Telegram бота у основы, а они на резерве; строки карточки резерва у обеих
сторон; всплывашка «📤 Вернуть бота на «Beta»»."""
from __future__ import annotations

from ..fmt import _e
from .common import _n, _when

USER_MAINTENANCE = "🔧 Бот временно недоступен, VPN работает нормально"
GIVE_TOAST = "Возвращаю бота на резерв"


def card_lines(v: dict, name: str) -> list[str]:
    """Блок «📤 Твои пользователи» карточки основы, пока пульт у неё (данные — reserve_pult_view)."""
    if not v:
        return []
    at = _when(v.get("at") or "")
    lines = ["└ 📍 Управление ботом: здесь" + (f" с {_e(at)}" if at else "")
             + " — резервный экземпляр не ответил. Пользователям отвечаю «бот временно недоступен, VPN работает нормально»"]
    if v.get("giving"):
        lines.append(f"└ ⏳ возвращаю бота на {_n(name)}")
    return lines


def peer_card_line(name: str) -> str:
    """Блок «📥 Пользователи …» карточки резерва: их Telegram у основы, сами они здесь."""
    return f"└ ⚠️ Telegram бота {_n(name)} у него — пользователи здесь"
