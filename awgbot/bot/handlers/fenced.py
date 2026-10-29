"""handlers/fenced.py — загородка лишённого кворума лидера (кворум резерва,
концепт «кворум» §6.3): админу доступен только раздел «🛟 Резерв» и его
действия (карточка, переезд кнопкой, возврат, С9); всё остальное —
всплывашка Т-К10. Роутер стоит ДО админских, как загородка гостя; действует
только пока сервисы говорят «лишён кворума» (reserve_quorum_fenced)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import (BroadcastCB, ClientCB, DeviceCB, GwJoinCB, GwMarkCB, GwSlotCB, Menu, PeriodCB,
                                  RoutingCB, SetCB, UpdateCB)
from awgbot.bot.filters import RoleFilter
from awgbot.bot.texts.reserve import quorum as qt

router = Router(name="fenced")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

# разделы настроек, доступные лишённому кворума: корень и «🛟 Резерв»
FENCED_SECTIONS = frozenset({"root", "rsv"})
MENU_ALLOWED = frozenset({"main", "refresh"})


def _fenced(services) -> bool:
    fn = getattr(services, "reserve_quorum_fenced", None)
    try:
        return bool(fn()) if fn is not None else False
    except Exception:                                         # noqa: BLE001
        return False


async def _refuse(cb: CallbackQuery) -> None:
    await cb.answer(qt.ADMIN_FENCED, show_alert=True)


def _fenced_filter(cb: CallbackQuery, services, **_) -> bool:
    return _fenced(services)


@router.callback_query(_fenced_filter, SetCB.filter(~F.sec.in_(FENCED_SECTIONS)))
async def settings_section(cb: CallbackQuery, **_):
    await _refuse(cb)


@router.callback_query(_fenced_filter, Menu.filter(~F.action.in_(MENU_ALLOWED)))
async def menu(cb: CallbackQuery, **_):
    await _refuse(cb)


for _cb in (UpdateCB, GwJoinCB, GwMarkCB, GwSlotCB, ClientCB, DeviceCB, PeriodCB, RoutingCB, BroadcastCB):
    @router.callback_query(_fenced_filter, _cb.filter())
    async def _any(cb: CallbackQuery, **_):
        await _refuse(cb)


@router.message(_fenced_filter, F.document, StateFilter(None))
async def document(message: Message, **_):
    await message.answer(qt.ADMIN_FENCED)


__all__ = ["router", "FENCED_SECTIONS"]
