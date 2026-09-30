"""
handlers/admin/updates.py — обновления бота (self-update): установка, меню
итога, выключение уведомлений.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from awgbot.bot.callbacks import UpdateCB
from awgbot.bot.handlers import updates_flow
from awgbot.bot.handlers.common import show_main_menu
from awgbot.bot.handlers.admin.panel import _return_panel

router = Router(name="admin.updates")


# ── Обновления бота (self-update) ────────────────────────────────────────────

async def _main_menu(message, services):
    await show_main_menu(message, services, "admin")


@router.callback_query(UpdateCB.filter(F.action == "install"))
async def update_install(cb: CallbackQuery, services):
    """«Обновить» — общий поток обеих ролей (updates_flow.install)."""
    await updates_flow.install(cb, services, return_panel=_return_panel)


@router.callback_query(UpdateCB.filter(F.action == "menu"))
async def update_menu(cb: CallbackQuery, services, state: FSMContext):
    await updates_flow.menu(cb, services, state, return_panel=_main_menu)


@router.callback_query(UpdateCB.filter(F.action == "mute"))
async def update_mute(cb: CallbackQuery, services):
    await updates_flow.mute(cb, services)
