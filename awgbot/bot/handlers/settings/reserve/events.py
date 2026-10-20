"""events.py — события канала пары в чат: подключился / заново / с двух адресов
(хвосты Т-Р2), код у принявшего (Т-Р6), не совпал, отказ, срок, итог (Т-Р8)
и аварийный код (Т-Р18) со звуком, сообщение с кодом — удаляется через
10 минут."""
from __future__ import annotations

import asyncio
import logging

from awgbot.bot import keyboards as kb
from awgbot.bot.keyboards import reserve as rkb
from awgbot.bot.texts.reserve import notices, pairing as pt, section as st_t
from awgbot.core import config
from awgbot.domain.services import ServiceError

log = logging.getLogger("awgbot.handlers.settings")
CODE_TTL = 10 * 60


async def _edit(bot, st: dict, text: str, markup=None) -> None:
    msg = st.get("msg") or []
    if len(msg) != 2:
        return
    try:
        await bot.edit_message_text(text, chat_id=int(msg[0]), message_id=int(msg[1]), reply_markup=markup)
    except Exception as e:                                # noqa: BLE001 — удалено, не изменилось
        if "not modified" not in str(e):
            log.info("резерв: живое сообщение не поправлено: %s", e)


async def _delete_later(bot, chat_id: int, message_id: int, delay: int = CODE_TTL) -> None:
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:                                     # noqa: BLE001
        pass


async def send_code(bot, services, *, renewed_why: str = "") -> None:
    """Аварийный код — отдельным сообщением со звуком, удаляется через 10 минут."""
    code = await asyncio.to_thread(services.reserve_code)
    if not code:
        return
    text = notices.code_renewed(code, renewed_why) if renewed_why else notices.code_issued(code)
    try:
        sent = await bot.send_message(config.ADMIN_ID, text, reply_markup=kb.hide_only())
    except Exception as e:                                # noqa: BLE001
        log.warning("резерв: код не отправлен: %s", e)
        return
    await asyncio.to_thread(services.reserve_code_shown)
    asyncio.get_running_loop().create_task(_delete_later(bot, sent.chat.id, sent.message_id))


async def on_event(bot, services, event: str, st: dict, extra: dict) -> None:
    role = str((st or {}).get("role") or "inviter")
    if event in ("connected", "reconnected", "two"):
        await _edit(bot, st, pt.invite_message(st, str(st.get("token") or "")), rkb.invite_message(str(st.get("id") or "")))
        return
    if event == "code":
        await _edit(bot, st, pt.code_message(str(st.get("peer_name") or "второго сервера"), str(extra.get("code") or "")),
                    rkb.accept_message())
        return
    if event == "wrong":
        await _edit(bot, st, pt.code_message(str(st.get("peer_name") or "второго сервера"), "") + "\n\n"
                    + pt.code_wrong(int(extra.get("left") or 0)), rkb.accept_message())
        return
    if event == "linked":
        peer = st or {}
        name, bot_user = str(peer.get("name") or "резерв"), str(peer.get("bot_username") or "")
        inv_msg = extra.get("msg")
        if inv_msg:
            await _edit(bot, {"msg": inv_msg}, notices.linked(name, bot_user), rkb.final())
        await send_code(bot, services)
        return
    if event == "expired":
        text = pt.invite_expired(st) if role == "inviter" else notices.refused_by_inviter("expired")
    elif event == "cancel":
        text = pt.INVITE_CANCELLED if role == "inviter" else notices.refused_by_inviter("cancel")
    elif event == "code_refused":
        text = pt.CODE_REFUSED if role == "inviter" else notices.refused_by_inviter("code")
    elif event == "replaced":
        text = pt.INVITE_STALE if role == "inviter" else notices.refused_by_inviter("replaced")
    else:
        text = notices.refused_by_inviter(event)
    await _edit(bot, st, text, rkb.final())


def make_on_event(bot, services):
    async def _on_event(event: str, st: dict, extra: dict) -> None:
        try:
            await on_event(bot, services, event, st or {}, extra or {})
        except ServiceError as e:
            log.info("резерв: событие «%s»: %s", event, e)
    return _on_event


__all__ = ["make_on_event", "on_event", "send_code", "st_t"]
