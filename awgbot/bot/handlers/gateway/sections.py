"""sections.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.bot import keyboards as kb
from awgbot.core import config
from awgbot.bot import texts
from awgbot.bot.roles import GATEWAY
from awgbot.bot.callbacks import CancelCB, GwCB
from awgbot.bot.handlers.common import call, edit_nav
from .panel import router
from awgbot.bot.handlers import gateway as pkg       # имена пакета — через него: их подменяют тесты


# ── ролевые разделы агента: SSH-доступ и VPN-транзит; общие — bot/sections ──

def _ssh_section(services):
    from awgbot.bot import paging
    st = services.ssh_screen()
    return (texts.gateway_ssh_text(st, pkg._bot_username(services)),
            kb.gateway_ssh_kb(st, page=paging.page_of(config.ADMIN_ID, "gwssh")))


_SECTIONS = {"ssh": _ssh_section}


async def _section(sec: str, services, key: str = ""):
    """Экран ролевого раздела агента — для sections.screen (br.role_screens)
    и реестра экранов: «ssh» и «lan»; прочее — панель."""
    if sec == "lan":
        return await pkg._transit_screen(services, config.ADMIN_ID)
    if sec in pkg._SECTIONS:
        return await call(pkg._SECTIONS[sec], services)
    return await pkg._panel_parts(services)


async def _render(cb: CallbackQuery, services, sec: str) -> None:
    await edit_nav(cb, services, *await pkg._section(sec, services))


async def _shared(cb: CallbackQuery, callback_data: GwCB, services, state=None) -> bool:
    """Колбэк общего раздела — диспетчеру sections (из тестов, которые зовут
    прежние обработчики напрямую); True — обработан."""
    from awgbot.bot import sections as secs
    packed = secs.resolve(GATEWAY, callback_data.pack())
    if packed is None:
        return False
    await secs.handle(cb, packed, services, state, GATEWAY)
    return True


async def gw_settings(cb: CallbackQuery, services, state: FSMContext):
    await pkg._shared(cb, GwCB(action="settings"), services, state)


async def gw_section(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    if not await pkg._shared(cb, callback_data, services, state):
        await pkg.gw_ssh_section(cb, services, state)


async def gw_toggle(cb: CallbackQuery, callback_data: GwCB, services):
    await pkg._shared(cb, callback_data, services)


async def gw_edit(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    await pkg._shared(cb, callback_data, services, state)


async def gw_cycle(cb: CallbackQuery, callback_data: GwCB, services):
    await pkg._shared(cb, callback_data, services)


async def gw_email_action(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    await pkg._shared(cb, callback_data, services, state)


async def gw_bot_restart(cb: CallbackQuery, services):
    await pkg._shared(cb, GwCB(action="botrestart!"), services)


async def gw_cancel_inline(cb: CallbackQuery, callback_data: CancelCB, services, state: FSMContext):
    from awgbot.bot.handlers.reply_commands import on_cancel_inline
    await on_cancel_inline(cb, callback_data, state, services, role="admin")


@router.callback_query(GwCB.filter(F.action == "ssh"))
async def gw_ssh_section(cb: CallbackQuery, services, state: FSMContext):
    await state.clear()
    await pkg._render(cb, services, "ssh")
    await cb.answer()

