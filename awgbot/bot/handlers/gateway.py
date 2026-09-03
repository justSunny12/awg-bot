"""
handlers/gateway.py — панель агента шлюза (роль gateway, только админ).

Один экран и одна кнопка: этап 1 — наблюдаемость. Пробы блокирующие
(subprocess) — через call, как всё синхронное в проекте.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import Menu
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import call, edit

router = Router(name="gateway")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))


@router.message(CommandStart())
async def gw_start(message: Message, services):
    st = await call(services.status)
    await message.answer(texts.gateway_panel(st), reply_markup=kb.gateway_panel_kb())


@router.callback_query(Menu.filter(F.action == "gw_refresh"))
async def gw_refresh(cb: CallbackQuery, services):
    st = await call(services.status)
    await edit(cb, texts.gateway_panel(st), kb.gateway_panel_kb())
    await cb.answer("Обновлено")
