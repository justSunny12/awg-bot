"""snapshot.py — часть пакета lists (см. __init__): производитель снимка события
для хвоста «и ещё N».

Одна схема на все источники (итоги ввода, отчёты, уведомления): список не влез
→ снимок в базе (infra/db/listsnap) → параметр /start `list-snap-<ключ>` для
хвоста (more_link / more_line у текста; метку «-n» следу и уведомлению ставит
links.mark_new). Без имени бота снимок не пишется: ссылки в тексте всё равно
не будет (deep_link без имени даёт текст), а запись лежала бы в базе зря и
занимала место в двадцати снимках чата. Не записался (сбой базы) — пусто,
хвост у текста остаётся текстом. Решает, влез ли список, вызывающий — по
пределу текста, который этот список рисует."""
from __future__ import annotations

from awgbot.util import listlink


def snap_param_sync(db, bot_username: str, chat: int, role: str, title: str, rows, back: str = "",
                    new: bool = False, n: int | None = None, missing: int = 0, tail_note: str = "") -> str:
    """Параметр /start снимка или пусто. Синхронный — для домена (Т-Ж4 в
    reserve/journal/tail пишет снимок из сервиса); обработчики зовут snap_param.
    tail_note — чем кончить строку «… и ещё N — …» снимка, когда остаток
    списка лежит не в журнале этого бота."""
    if not bot_username:
        return ""
    key = db.list_snap_keep(int(chat), role, title, list(rows), back, new, n, missing, tail_note)
    return listlink.build("snap", key=key) if key else ""


async def snap_param(services, chat: int, role: str, title: str, rows, back: str = "", new: bool = False,
                     n: int | None = None, missing: int = 0) -> str:
    """То же из обработчика: база — в потоке (call), имя бота — у сервисов."""
    from awgbot.bot.handlers.common import call
    return await call(snap_param_sync, services.db, getattr(services, "bot_username", "") or "", chat, role, title,
                      rows, back, new, n, missing)


async def snap_codes(message, services, title: str, items: list, back: str, shown: int) -> tuple | None:
    """Итог ввода адресов (SSH-доступ обоих ботов): больше shown — все снимком
    строками <code>; (имя бота, параметр /start) для хвоста more(link=…) или
    None — хвост текстом."""
    from awgbot.bot.texts.fmt import _e
    if len(items) <= shown:
        return None
    param = await snap_param(services, message.chat.id, "admin", title, [f"<code>{_e(x)}</code>" for x in items], back)
    return (getattr(services, "bot_username", "") or "", param) if param else None
