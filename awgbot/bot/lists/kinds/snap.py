"""snap.py — вид snap: снимок списка события (итог действия, отчёт, уведомление),
который не влез, — из базы (infra/db/listsnap). Есть у обеих установок; роль —
та, что создала снимок, и открыть его можно только из того же чата.

Шапка — «<заголовок> · <N> · на 27.10 14:05 (UTC+3)»: данные на момент
события, а не открытия. «⬅️ Назад» — на экран-источник, если снимок его знает,
иначе в меню роли. Протухший — «📋 Список от … больше не хранится», чужой или
снятый — «📋 Этого списка больше нет»."""
from __future__ import annotations

from awgbot.bot.handlers.common import call
from awgbot.bot.texts import lists as tl

from .. import Kind, View, register

ROLES = ("admin", "client", "invited")


async def snap(services, ref: int, key: str, chat_id: int) -> View | None:
    from awgbot.util import timeutil
    data = await call(services.db.list_snap_get, key, chat_id) if key else None
    back = _back(services, data)
    if data is None:
        return View(title="", empty=tl.SNAP_GONE, back=back, live=False)
    if data.get("stale"):
        try:
            at = timeutil.fmt_dt_ui(timeutil.parse_iso(str(data.get("at") or "")))
        except ValueError:
            at = ""
        return View(title="", empty=tl.snap_stale(at), back=back, live=False)
    rows = [str(r) for r in data.get("rows") or []]
    try:
        at = timeutil.fmt_dt_ui(timeutil.parse_iso(str(data.get("at") or "")))
    except ValueError:
        at = ""
    n = int(data.get("n") or len(rows))
    return View(title=str(data.get("title") or ""), rows=rows, count=str(n) if n else "", back=back, live=False,
                empty=tl.SNAP_GONE, at=f"на {at}" if at else "")


def _back(services, data: dict | None) -> tuple | None:
    """«⬅️ Назад» на экран-источник (колбэк в снимке); без него — None: «⬅️ В меню»
    роли того, кто смотрит, подставит экран (bot/lists render)."""
    from awgbot.bot import ui
    packed = str((data or {}).get("back") or "")
    return ui.back(packed) if packed else None


register(Kind("snap", "*", ROLES, snap))
