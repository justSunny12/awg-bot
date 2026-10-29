"""retire.py — часть пакета lists/msgs: прежнее меню гаснет.

Ниже пришло новое меню. У прежнего первой строкой стоял итог (информационная
строка, screens.with_note) — оно правится до одного итога и одной кнопки
«Скрыть» и дальше живёт обычным следом (запись ведёт посредник: ссылки итога
с меткой «-n», «Скрыть» — на сутки). Меню старше суток — итог без кнопок:
ночью её бы сняли, а после 48 часов она бы не удаляла. Меню без итога
гаснет, как прежде: кнопки долой, текст остаётся; drop — удаляется (запасной
ход, когда следующий экран приходит по внешнему событию). Правка не удалась
— как прежде."""
from __future__ import annotations

import logging

from aiogram.types import LinkPreviewOptions

log = logging.getLogger(__name__)

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def retire(bot, services, chat: int, mid: int, *, drop: bool = False) -> None:
    from awgbot.bot import keyboards as kb
    from awgbot.bot import links
    from awgbot.bot.handlers.common import call
    from awgbot.infra.db.listsnap import hide_due
    chat, mid = int(chat), int(mid)
    rec = None
    try:
        rec = await call(services.db.list_msg_get, chat, mid)
    except Exception as e:                                 # noqa: BLE001 — гашение важнее учёта
        log.warning("lists: запись сообщения %d чата %d не прочитана: %s", mid, chat, e)
    note = str((rec or {}).get("note") or "")
    if note:
        from . import NOTE
        tok = NOTE.set("")                                 # итог нового меню — не этой правке
        try:
            await bot.edit_message_text(chat_id=chat, message_id=mid, text=links.mark_new(note),
                                        reply_markup=None if hide_due(rec) else kb.hide_only(),
                                        link_preview_options=_NO_PREVIEW)
            log.info("lists: сообщение %d чата %d — меню с итогом оставлено следом", mid, chat)
            return
        except Exception:                                  # noqa: BLE001 — как прежде: снять кнопки
            try:
                await call(services.db.list_msg_drop_note, chat, mid)   # меню уже не станет следом
            except Exception:                              # noqa: BLE001
                pass
        finally:
            NOTE.reset(tok)
    if drop:
        try:
            await bot.delete_message(chat_id=chat, message_id=mid)
            return
        except Exception:                                  # noqa: BLE001
            pass
    try:
        await bot.edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=None)
    except Exception:                                      # noqa: BLE001 — удалено/старое: не страшно
        pass
