"""guest.py — раздел «🛟 Резерв» в боте основы на резерве (гостевой режим):
карточка со статусом (Т-РГ3) и «↩️ Вернуть клиентов», когда основа снова в
строю и рычаг «по кнопке» (сам возврат — этап возврата, здесь — экран)."""
from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery

from awgbot.bot.callbacks import RsvCB
from awgbot.bot.handlers.common import call
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.texts.reserve import guest as gt
from awgbot.core import config

from .._router import router
from ..render import _render


async def settings_root_text(services) -> str:
    """Текст корня «⚙️ Настроек» на резерве — карточка резерва (Т-РГ3); GUEST.root_text."""
    return gt.settings_text(await call(services.guest_screen_data))


async def guest_screen(services):
    d = await call(services.guest_screen_data)
    return gt.card_text(d), rkb.guest_card(can_return=bool(await call(services.guest_can_return)))


@router.callback_query(RsvCB.filter(F.action == "refresh"))
async def refresh(cb: CallbackQuery, services, **_):
    if not config.GUEST:
        await cb.answer()
        return
    await _render(cb, "rsv", services)
    await cb.answer()
