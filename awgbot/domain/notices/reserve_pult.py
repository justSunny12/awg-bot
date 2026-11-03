"""reserve_pult.py — часть пакета notices (см. __init__): уведомления «пульт у
основы» (domain/services/reserve/pult) — гость не отвечает в
Telegram, линк пары молчит, и основа забрала Telegram своего бота себе, пока
пользователи остаются на резерве. Общие правила — как у reserve: имена серверов —
«имя», у многострочных — заголовок без точки, середина с точкой, без точки в конце."""
from __future__ import annotations

from awgbot.util.textfmt.base import plural_ru

from .reserve_fmt import _n


def pult_taken(reserve: str, minutes: int) -> str:
    """Админу основы, со звуком: Telegram бота снова здесь, пользователи — по-прежнему на резерве."""
    m = f"{minutes} {plural_ru(minutes, 'минуту', 'минуты', 'минут')}"
    return (f"🎛 Telegram бота снова у этого сервера\n"
            f"Бот на {_n(reserve)} не отвечал в Telegram {m}, связи с {_n(reserve)} нет.\n"
            f"Пользователи по-прежнему на {_n(reserve)} — VPN у них работает, в боте им отвечаю «бот на техработах».\n"
            "Дальше решаешь ты — «🛟 Резерв»: «↩️ Вернуть пользователей сюда» или «📤 Отдать Telegram резерву»")


def pult_handed(reserve: str) -> str:
    """Админу основы: вебхук снова у бота на резерве (Telegram отдан или забран им сам)."""
    return f"📤 Telegram бота снова у {_n(reserve)} — бот отвечает оттуда"


def pult_at_primary(primary: str) -> str:
    """Админу резерва, один раз на отметку пульта: Telegram бота основы у неё, её пользователи здесь."""
    return f"⚠️ Telegram бота {_n(primary)} на своём сервере — пользователи здесь"
