"""dns.py — «🌐 Запись DNS» (Т-Р16): провайдер циклом, токен (сообщение
удаляется сразу после приёма), проверка записи у провайдера и у
авторитетного сервера."""
from __future__ import annotations

import html

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import ui
from awgbot.bot.callbacks import RsvCB
from awgbot.bot.handlers.common import ask_tracked, call, cleanup_content, edit, send_menu
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.states import ReserveDnsToken
from awgbot.bot.texts.reserve import section as st_t
from awgbot.domain.services import ServiceError
from awgbot.domain.services.reserve.dns import PROVIDERS

from .._router import router
from ..render import _render


async def dns_screen(services):
    d = await call(services.reserve_dns_screen_data)
    return st_t.dns_text(d), rkb.dns(provider_label=d.get("provider_label") or "вручную", has_name=bool(d.get("name")),
                                     manual=d.get("provider") == "manual")


@router.callback_query(RsvCB.filter(F.action == "dns"))
async def dns_open(cb: CallbackQuery, services, state: FSMContext, **_):
    await state.clear()
    await _render(cb, "rsv_dns", services)
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "dns_prov"))
async def dns_provider(cb: CallbackQuery, services, **_):
    cur = await call(services.reserve_dns_provider)
    nxt = PROVIDERS[(PROVIDERS.index(cur) + 1) % len(PROVIDERS)]
    try:
        await call(services.reserve_dns_set_provider, nxt)
    except ServiceError as e:
        await cb.answer(ui.toast_cap(e, html=getattr(e, "html", False)), show_alert=True)
        return
    await _render(cb, "rsv_dns", services)
    await cb.answer()


@router.callback_query(RsvCB.filter(F.action == "dns_token"))
async def dns_token_ask(cb: CallbackQuery, services, state: FSMContext, **_):
    d = await call(services.reserve_dns_screen_data)
    if d.get("provider") == "manual":
        await cb.answer("Провайдер «вручную» — токен не нужен", show_alert=True)
        return
    labels = str(d.get("name") or "").split(".")
    zone = ".".join(labels[-3:]) if len(labels) >= 3 and len(labels[-2]) <= 3 else ".".join(labels[-2:])   # vpn.example.co.uk → example.co.uk
    await state.set_state(ReserveDnsToken.value)
    await edit(cb, st_t.dns_token_prompt(str(d.get("provider") or ""), zone), rkb.dns_token_prompt())
    await cb.answer()


@router.message(ReserveDnsToken.value, F.text)
async def dns_token_received(message: Message, services, state: FSMContext):
    text = (message.text or "").strip()
    try:
        await message.delete()                            # токен в чате не держим
    except Exception:                                     # noqa: BLE001
        pass
    try:
        await call(services.reserve_dns_set_token, text)
    except ServiceError as e:
        await ask_tracked(message, services, f"⚠️ {ui.toast_cap(e, html=getattr(e, "html", False))}", reply_markup=rkb.dns_token_prompt())
        return
    await state.clear()
    await cleanup_content(message.bot, services, message.chat.id)
    res = await call(services.reserve_dns_check)
    text_r, markup = await dns_screen(services)
    await ask_tracked(message, services, ("✅ " if res.get("ok") else "⚠️ ") + html.escape(str(res.get("text") or ""), quote=False))
    await send_menu(message, services, text_r, markup)   # прежнее меню (приглашение к вводу) гаснет


@router.callback_query(RsvCB.filter(F.action == "dns_check"))
async def dns_check(cb: CallbackQuery, services, **_):
    res = await call(services.reserve_dns_check)
    await _render(cb, "rsv_dns", services)
    await cb.answer(("✅ " if res.get("ok") else "⚠️ ") + str(res.get("text") or "")[:180], show_alert=not res.get("ok"))
