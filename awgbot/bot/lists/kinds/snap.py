"""snap.py — вид snap: снимок списка события (итог действия, отчёт, уведомление),
который не влез, — из базы (infra/db/listsnap). Есть у обеих установок и у
всех ролей; открыть снимок можно только из чата, где он создан (роль
создателя лежит в снимке справочно, экран её не сверяет).

Шапка — «<заголовок> · <N> · стр. k из m · от 27.10 14:05 (UTC+3)»: данные
на момент события, а не открытия. «⬅️ Назад» — на экран-источник, если
снимок его знает, иначе «⬅️ В меню» роли того, кто смотрит. Протухший или
вытесненный — «📋 Список от … больше не хранится», чужой или несуществующий —
«📋 Этого списка больше нет»."""
from __future__ import annotations

from awgbot.bot.handlers.common import call
from awgbot.bot.texts import lists as tl

from .. import Kind, View, register

ROLES = ("admin", "client", "invited")


async def snap(services, ref: int, key: str, chat_id: int) -> View | None:
    from awgbot.util import timeutil
    data = await call(services.db.list_snap_get, key, chat_id) if key else None
    back = _back(data)
    if data is None:
        return View(title="", empty=tl.SNAP_GONE, back=back, live=False)
    try:
        at = timeutil.fmt_dt_ui(timeutil.parse_iso(str(data.get("at") or "")))
    except ValueError:
        at = ""
    if data.get("stale"):
        return View(title="", empty=tl.snap_stale(at, str(data.get("why") or "")), back=back, live=False)
    rows = [str(r) for r in data.get("rows") or []]
    n = int(data.get("n") or len(rows))
    return View(title=str(data.get("title") or ""), rows=rows, count=str(n) if n else "", back=back, live=False,
                empty=tl.SNAP_GONE, at=f"от {at}" if at else "")


def _back(data: dict | None) -> tuple | None:
    """«⬅️ Назад» на экран-источник (колбэк в снимке); без него — None: «⬅️ В меню»
    роли того, кто смотрит, подставит экран (bot/lists render)."""
    from awgbot.bot import ui
    packed = str((data or {}).get("back") or "")
    return ui.back(packed) if packed else None


register(Kind("snap", "*", ROLES, snap))
