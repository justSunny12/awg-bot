"""peeradmin.py — роль админа второго сервера: экран Т-РН1 по /start и
«🔄 Обновить», «🙈 Скрыть». Ни одного обработчика, пишущего в данные этого
сервера: роль — только чтение сведений о паре; ввод аварийного кода в Н4 —
в peercode.py (его роутер стоит перед этим). Роутер — только под
RoleFilter("peer_admin")."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import PeerCB
from awgbot.bot.filters import PeerAdminFilter, RoleFilter
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.texts.reserve import peeradmin as pt

router = Router(name="peeradmin")
router.message.filter(RoleFilter("peer_admin"))
router.callback_query.filter(PeerAdminFilter())


async def _screen(services, role: str = "peer_admin"):
    d = await call(services.reserve_peer_admin_summary)
    return pt.screen(d), rkb.peer_admin(code_needed=bool(d.get("code_needed")), has_client=bool(d.get("has_client")),
                                        in_card=(role == "admin"))


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
async def refresh(cb: CallbackQuery, services, role: str = "peer_admin", **_):
    text, markup = await _screen(services, role)
    await edit(cb, text, markup)
    await cb.answer("Обновлено")


@router.callback_query(PeerCB.filter(F.action == "client"))
async def as_client(cb: CallbackQuery, services, **_):
    """«👤 Меню пользователя»: он ещё и пользователь этого сервера — на время сессии
    middleware ведёт его как пользователя; экран роли вернётся сам через полчаса."""
    from awgbot.bot import screens
    from awgbot.bot.handlers.common import send_menu
    from awgbot.core import access_cache
    uid = int(cb.from_user.id)
    client = await call(services.reserve_peer_admin_client)
    if client is None or int(getattr(client, "tg_id", 0) or 0) != uid:
        await cb.answer("Ты не пользователь этого сервера", show_alert=True)
        return
    access_cache.peer_client_on(uid)
    parts = await screens.render("main", services=services, role="client", client=client, chat_id=cb.message.chat.id)
    await cb.answer()
    if parts is None:
        access_cache.peer_client_off(uid)
        return
    await send_menu(cb.message, services, parts[0], parts[1])
