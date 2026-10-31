"""handlers/guest.py — загородка бота основы на резерве (гостевой режим):
всё, что действует на хост основы, отвечает всплывашкой Т-РГ2 и остаётся
без изменений. Роутер стоит ДО админских: шлюзы (слоты, подключение,
пометка), обновления, разделы настроек кроме «🛟 Резерв», переезд профилей,
восстановление по присланной копии, действия пары. Обработчики пользователей
сюда не доходят: фильтр роли — admin.

Кнопки уведомлений (NoteCB) разбирает reply_commands — он стоит раньше этого
роутера; хостовые из них («↩️ Вернуть запись» — запись DNS на адрес основы,
«🔁 Повторить» — догон шлюзов) он отклоняет по GUEST_NOTES_REFUSED той же
всплывашкой Т-РГ2. Загородка гостя — перечень хостового: админ на резерве
по-прежнему ведёт профили и устройства; страховка за ней — HOST_ONLY в сервисах."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import GwJoinCB, GwMarkCB, GwSlotCB, RsvCB, SetCB, UpdateCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.texts.reserve import guest as gt

router = Router(name="guest")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

# разделы настроек, которые есть у гостя; остальные — хост основы
GUEST_SECTIONS = frozenset({"root", "rsv"})
# действия карточки резерва на резерве: открыть и вернуть пользователей (этап возврата)
GUEST_RSV_ACTIONS = frozenset({"open", "back", "back_yes", "refresh"})
# кнопки уведомлений, которые действуют на хост основы
GUEST_NOTES_REFUSED = frozenset({"rsv_record_back", "gw_retry"})


async def refuse(cb: CallbackQuery) -> None:
    """Т-РГ2 всплывашкой; экран и кнопки остаются без изменений."""
    await cb.answer(gt.UNAVAILABLE, show_alert=True)


@router.callback_query(SetCB.filter(~F.sec.in_(GUEST_SECTIONS)))
async def settings_section(cb: CallbackQuery, **_):
    await refuse(cb)


@router.callback_query(RsvCB.filter(~F.action.in_(GUEST_RSV_ACTIONS)))
async def reserve_action(cb: CallbackQuery, **_):
    await refuse(cb)


@router.callback_query(UpdateCB.filter())
async def updates(cb: CallbackQuery, **_):
    await refuse(cb)


@router.callback_query(GwJoinCB.filter())
async def gw_join(cb: CallbackQuery, **_):
    await refuse(cb)


@router.callback_query(GwMarkCB.filter())
async def gw_mark(cb: CallbackQuery, **_):
    await refuse(cb)


@router.callback_query(GwSlotCB.filter())
async def gw_slot(cb: CallbackQuery, **_):
    await refuse(cb)


@router.message(F.document, StateFilter(None))
async def document(message: Message, **_):
    """Файл в чате админа — это восстановление копии или конфигурация шлюза:
    на резерве ни того, ни другого."""
    await message.answer(gt.UNAVAILABLE)


__all__ = ["router", "GUEST_SECTIONS", "GUEST_RSV_ACTIONS", "GUEST_NOTES_REFUSED", "refuse"]
