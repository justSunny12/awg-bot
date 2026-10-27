"""server.py — виды основного бота вне слотов (админ): имена SSH-доступа, которые
не резолвятся, и кто потеряет связь при завершении переезда профилей."""
from __future__ import annotations

from awgbot.bot import ui
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers.common import call
from awgbot.bot.texts import lists as tl

from .. import Kind, View, register

ADMIN = ("admin",)


async def fwunres(services, ref: int, key: str, chat_id: int) -> View | None:
    from awgbot.bot.texts.fmt import _e
    st = await call(services.firewall_screen)
    names = list(st.get("unresolved") or [])
    return View(title=tl.FWUNRES_TITLE, rows=[f"<code>{_e(n)}</code>" for n in names],
                count=str(len(names)) if names else "", empty=tl.UNRES_EMPTY, back=ui.back(SetCB(sec="fw")))


async def migdrop(services, ref: int, key: str, chat_id: int) -> View | None:
    """«профиль — устройство»: недавно активные, кто ещё не подключился на новой
    конфигурации, — то же, что называет подтверждение «🚚 Завершить переезд?»."""
    from awgbot.bot.texts.fmt import _e
    if not await call(services.migration_running):
        return View(title="", empty=tl.MIGRATION_NOT_RUNNING, back=ui.back(SetCB(sec="svc")))
    _pairs, dropped = await call(services.migration_finish_preview)
    return View(title=tl.MIGDROP_TITLE, rows=[_e(x) for x in dropped], count=str(len(dropped)) if dropped else "",
                empty=tl.MIGDROP_EMPTY, back=ui.back(SetCB(sec="mig", act="do", key="finish")))


register(Kind("fwunres", "main", ADMIN, fwunres))
register(Kind("migdrop", "main", ADMIN, migdrop))
