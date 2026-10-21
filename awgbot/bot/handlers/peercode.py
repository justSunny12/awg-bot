"""peercode.py — ввод аварийного кода в роли админа второго сервера (Н4,
Т-РН2, Т-РН3): кнопка «🔑 Ввести аварийный код» → приглашение → код текстом.
Код принимается только от этого аккаунта (RoleFilter) и только пока приём
открыт; сообщение с кодом удаляется до ответа; код уходит транзитом в
процесс открытия — бот его не хранит. Роутер стоит перед peeradmin, иначе
текст с кодом ловил бы общий ответ роли."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import PeerCB
from awgbot.bot.filters import PeerAdminFilter
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.states import PeerCode
from awgbot.bot.texts.reserve import peeradmin as pt

router = Router(name="peercode")
router.message.filter(PeerAdminFilter())                  # только код в состоянии PeerCode
router.callback_query.filter(PeerAdminFilter())


@router.message(PeerCode.value, F.text.startswith("/"))
async def code_command(message: Message, state: FSMContext, services, **_):
    """Команда вместо кода (/start, передумал): ввод закрыт, экран роли — попытка не в счёт."""
    await state.clear()
    text, markup = await _role_screen(services)
    await message.answer(text, reply_markup=markup)


@router.message(PeerCode.value, F.text)
async def code_text(message: Message, state: FSMContext, services, **_):
    """Код из чата роли: удалить сообщение, проверить, итог (Т-РН3)."""
    from awgbot.runtime import rsvactions
    try:
        await message.delete()                            # код в чате не держим
    except Exception:                                     # noqa: BLE001
        pass
    data = await state.get_data()
    await state.clear()
    res = await call(services.reserve_code_in, int(message.from_user.id), message.text or "")
    if res.get("actions"):
        await rsvactions.run(res["actions"])
    peer = await call(services.reserve_peer_name)
    again = res.get("why") in ("wrong", "garbled") and (res.get("why") == "garbled" or int(res.get("left") or 0))
    prompt = data.get("prompt") or []
    if len(prompt) == 2 and not again:
        # приглашение к вводу — на месте экрана роли; после итога вернуть экран
        text, markup = await _role_screen(services)
        try:
            await message.bot.edit_message_text(text, chat_id=int(prompt[0]), message_id=int(prompt[1]), reply_markup=markup)
        except Exception:                                 # noqa: BLE001 — удалено, не изменилось
            pass
    await message.answer(pt.code_result(res, peer))
    if again:
        await state.set_state(PeerCode.value)
        await state.update_data(prompt=prompt)


async def _role_screen(services):
    from awgbot.bot.handlers.peeradmin import _screen
    return await _screen(services)


@router.callback_query(PeerCB.filter(F.action == "code"))
async def code_start(cb: CallbackQuery, state: FSMContext, services, **_):
    d = await call(services.reserve_code_prompt_data)
    window = await call(services.reserve_code_window)
    if not window.get("open"):
        await cb.answer("Приём кода закрыт — переезд не нужен или идёт сам", show_alert=True)
        return
    await state.set_state(PeerCode.value)
    await edit(cb, pt.code_prompt(d), rkb.peer_code_cancel())   # на месте экрана роли, не вторым сообщением
    await state.update_data(prompt=[cb.message.chat.id, cb.message.message_id])
    await cb.answer()


@router.callback_query(PeerCB.filter(F.action == "code_cancel"))
async def code_cancel(cb: CallbackQuery, state: FSMContext, services, role: str = "peer_admin", **_):
    await state.clear()
    d = await call(services.reserve_peer_admin_summary)
    await edit(cb, pt.screen(d), rkb.peer_admin(code_needed=bool(d.get("code_needed")), has_client=bool(d.get("has_client")),
                                                in_card=(role == "admin")))
    await cb.answer()
