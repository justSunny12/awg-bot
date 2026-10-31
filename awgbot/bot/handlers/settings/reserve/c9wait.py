"""c9wait.py — экран «не ждать» (ссылка «не ждать?» в плашке С9 карточки резерва,
/start rsv-c9): подтверждение и «🗑 Не ждать» — снятие отметки сервисом
(`reserve_c9_drop`), итог уведомлением; отметка уже снята — всплывашка и карточка."""
from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery

from awgbot.bot import ui
from awgbot.bot.callbacks import RsvCB
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.notifier import send_notifications
from awgbot.bot.texts.reserve import c9wait as ct
from awgbot.domain.services import ServiceError

from .._router import router
from ..render import _render

GONE_TOAST = "Отметки уже нет — изменений от бота на резерве не жду"


async def c9_screen(services):
    """(text, markup) экрана «не ждать»; отметки нет — пояснение и только «Назад»."""
    v = await call(services.reserve_c9_wait_view)
    name = await call(services.reserve_peer_name)
    return ct.screen_text(v, name), rkb.c9_wait(can_drop=bool(v))


@router.callback_query(RsvCB.filter(F.action == "c9_drop"))
async def c9_drop(cb: CallbackQuery, services):
    try:
        notes = await call(services.reserve_c9_drop)
    except ServiceError as e:
        await edit(cb, *await c9_screen(services))           # что известно о резерве — свежим
        await cb.answer(ui.toast_cap(e, html=getattr(e, "html", False)), show_alert=True)
        return
    await _render(cb, "rsv", services)
    if notes is None:                                         # повторное нажатие или снята фактом
        await cb.answer(GONE_TOAST)
        return
    await cb.answer("Больше не жду")
    await send_notifications(cb.bot, notes)
