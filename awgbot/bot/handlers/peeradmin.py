"""peeradmin.py — роль админа второго сервера: экран Т-РН1 по /start и
«🔄 Обновить», «🙈 Скрыть». Ни одного обработчика, пишущего в данные этого
сервера: роль — только чтение сведений о паре (ввод аварийного кода в Н4 —
этап голосования). Роутер — только под RoleFilter("peer_admin")."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import PeerCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.texts.reserve import peeradmin as pt

router = Router(name="peeradmin")
router.message.filter(RoleFilter("peer_admin"))
router.callback_query.filter(RoleFilter("peer_admin"))


async def _screen(services):
    d = await call(services.reserve_peer_admin_summary)
    return pt.screen(d), rkb.peer_admin(code_needed=bool(d.get("code_needed")))


@router.message(CommandStart())
async def start(message: Message, services, **_):
    await call(services.reserve_peer_admin_opened)
    text, markup = await _screen(services)
    await message.answer(text, reply_markup=markup)


@router.message(F.text)
async def any_text(message: Message, services, **_):
    text, markup = await _screen(services)
    await message.answer(text, reply_markup=markup)


@router.callback_query(PeerCB.filter(F.action == "refresh"))
async def refresh(cb: CallbackQuery, services, **_):
    text, markup = await _screen(services)
    await edit(cb, text, markup)
    await cb.answer("Обновлено")
