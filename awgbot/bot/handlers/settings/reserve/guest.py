"""guest.py — раздел «🛟 Резерв» в боте основы на резерве (гостевой режим):
карточка со статусом (Т-РГ3) и «↩️ Вернуть пользователей», когда основа снова в
строю и рычаг «по кнопке» (сам возврат — этап возврата, здесь — экран)."""
from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery

from awgbot.bot.callbacks import RsvCB
from awgbot.bot.handlers.common import call, edit
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


@router.callback_query(RsvCB.filter(F.action == "back"))
async def back_ask(cb: CallbackQuery, services, **_):
    """«↩️ Вернуть пользователей»: в боте основы на резерве — диалог Т-Р11; у основы
    (возврат по кнопке, Т-РУ6б) — ветка move.py. Один обработчик на оба: aiogram
    останавливается на первом совпавшем, и второй был бы недостижим."""
    if not config.GUEST:
        from . import move
        await move.back_ask(cb, services)
        return
    if not await call(services.guest_can_return):
        await cb.answer("Сервер ещё не в строю — вернуть пользователей пока нельзя", show_alert=True)
        return
    d = await call(services.guest_screen_data)
    await edit(cb, gt.return_dialog(d), rkb.confirm_return())
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "back_yes"))
async def back_go(cb: CallbackQuery, services, **_):
    if not config.GUEST:
        from . import move
        await move.back_go(cb, services)
        return
    from awgbot.runtime.pairlink import guestctl
    c = guestctl.client()
    if c is None or not await c.send("back_req"):
        await cb.answer("Нет связи с ботом резерва — попробуй через минуту", show_alert=True)
        return
    await _render(cb, "rsv", services)
    await cb.answer("Просьба о возврате отправлена")


@router.callback_query(RsvCB.filter(F.action == "refresh"))
async def refresh(cb: CallbackQuery, services, **_):
    if not config.GUEST:
        await cb.answer()
        return
    await _render(cb, "rsv", services)
    await cb.answer()
