"""c9wait.py — ожидание правок бота на резерве по отметке С9 без рычага: плашка
карточки резерва «жду события» (по сроку — со ссылкой «не ждать?») и экран
подтверждения «🗑 Не ждать» (данные — domain reserve_c9_wait_view)."""
from __future__ import annotations

from awgbot.util.textfmt.base import deep_link

from ..fmt import _e
from .common import _date, _n

C9_PAYLOAD = "rsv-c9"             # /start rsv-c9 — экран «не ждать» (ссылка в плашке карточки резерва)


def card_line(v: dict, name: str, bot_username: str = "") -> str:
    """Плашка блока «📤» карточки: отметка стоит; срок вышел и снять можно — «(не ждать?)»."""
    if not v:
        return ""
    tail = f" ({deep_link(bot_username, C9_PAYLOAD, 'не ждать?')})" if v.get("overdue") and not v.get("drop_why") else ""
    return f"└ ⏳ жду события с {_n(name)}{tail}"


def screen_text(v: dict, name: str) -> str:
    """Подтверждение «🗑 Не ждать»: что потеряется, что известно о резерве, когда ждать стоит."""
    n = _n(name)
    if not v:
        return f"🗑 <b>Изменений от бота на {n} не жду</b>\n\nЖдать нечего — отметка уже снята"
    since = _date(v.get("since") or "")
    if v.get("drop_why"):
        now = _e(v["drop_why"])
    elif v.get("online"):
        now = f"{n} на связи, но после возврата сюда ещё не сказал, что твоего бота у него нет"
    else:
        down = _date(v.get("down_since") or "")
        now = f"{n} не на связи" + (f" с {_e(down)}" if down else "") + " — несёт ли он ещё твой бот, неизвестно"
    moved = int(v.get("moved") or 0)
    lines = [f"⚠️ <b>Не ждать изменений от бота на {n}?</b>", "",
             f"После передачи бот на {n} мог работать: продления, правки профилей и устройств, сделанные в нём, "
             "этот сервер ждёт" + (f" с {_e(since)}" if since else "") + f" и перенесёт, когда {n} выйдет на связь.",
             "Если не ждать — такие изменения, если они были, будут потеряны и сюда не придут."
             + (f" Уже перенесено изменений: {moved}." if moved else ""),
             f"Сейчас {now}.",
             f"Ждать стоит, если {n} может вернуться — например, сервер перезагружают или чинят у провайдера"]
    return "\n".join(lines)


__all__ = ["C9_PAYLOAD", "card_line", "screen_text"]
