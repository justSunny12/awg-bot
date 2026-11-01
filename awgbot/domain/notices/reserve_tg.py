"""reserve_tg.py — часть пакета notices (см. __init__): пул адресов Telegram
(анонсы сетей) и доставка вебхука бота основы на резерв (Т-РУ19, Т-РУ25).
Правила — как у reserve."""
from __future__ import annotations

from awgbot.util import timeutil

from .reserve_fmt import _e, _n, _when


# ── пул адресов Telegram и вебхук (этап 7) ──────────────────────────────────

def tg_pool_stale(since: str) -> str:
    """Т-РУ25: анонсы сетей Telegram не обновлялись."""
    at = _date(since)
    return ("⚠️ Анонсы сетей Telegram не обновлялись" + (f" с {at}" if at else "")
            + ": источник не отвечает — пул вебхука прежний")


def tg_pool_core_lost(prefix: str) -> str:
    return (f"⚠️ Сеть <code>{_e(prefix)}</code> со страницы Bot API больше не анонсирует Telegram — пул её держит, "
            "но страница могла устареть")


def tg_pool_ok() -> str:
    return "🟢 Анонсы сетей Telegram снова обновляются"


def _date(iso: str) -> str:
    try:
        return timeutil.parse_iso(iso).strftime("%d.%m")
    except (ValueError, TypeError):
        return ""


def webhook_stalled_reserve(primary: str, since: str, error: str, port: int) -> str:
    """Т-РУ19 админу резерва: Telegram не доставляет обновления бота основы сюда."""
    return (f"⚠️ Telegram не доставляет обновления бота {_n(primary)} на этот сервер с {_when(since)}: "
            f"<code>{_e(error)}</code> — проверь, открыт ли у хостинга порт <code>{int(port)}</code>")


def webhook_foreign_reserve(primary: str) -> str:
    """Админу резерва: вебхук бота основы больше не у её бота здесь — Telegram забрала основа."""
    return (f"⚠️ Вебхук бота {_n(primary)} указывает не на этот сервер — Telegram у основы, бот основы здесь "
            "решения больше не принимает")


def webhook_restored_reserve(primary: str) -> str:
    return f"🟢 Доставка обновлений бота {_n(primary)} восстановлена"
