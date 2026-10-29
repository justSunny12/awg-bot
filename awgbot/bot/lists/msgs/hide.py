"""hide.py — часть пакета lists/msgs: нажатая «Скрыть».

Сообщение удалено — его снимки и запись прочь без отметки, тем же действием
(обязательство обработчика bot/handlers/hide, не надежда на посредник;
посредник видит удаление тоже — повтор безвреден, снятие идемпотентно)."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def on_hidden(services, chat: int, mid: int) -> list[str]:
    """Снимки и запись сообщения прочь; возвращает снятые ключи."""
    from awgbot.bot.handlers.common import call
    try:
        keys = await call(services.db.list_msg_forget, int(chat), int(mid), True)
    except Exception as e:                                 # noqa: BLE001 — сообщение уже удалено
        log.warning("lists: запись сообщения %d чата %d не снята: %s", int(mid), int(chat), e)
        return []
    if keys:
        log.info("lists: сообщение %d чата %d удалено — снимки %s сняты", int(mid), int(chat), keys)
    return keys
