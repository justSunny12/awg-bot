"""gwjoin.py — подключение шлюза командой в чате бота сервера AWG (§3.6–3.9
концепта «единые обновления»): сообщение подключения (Т-П1…Т-П3), ввод
кода (Т-П4…Т-П8), отмена (Т-П10, Т-П14), срок (Т-П11), не актуально (Т-П12),
итог (Т-П9).

Сообщение подключения — обычное сообщение, не живое меню: его правит ход
сопряжения, и навигация по боту не должна снимать с него «✏️ Ввести код».
Ввод кода относится только к заявке нажатой кнопки (Р20): слот и id
сопряжения — в данных диалога.
"""
from __future__ import annotations

import logging

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot import texts, ui
from awgbot.bot.callbacks import GwJoinCB
from awgbot.bot.handlers.common import ask_tracked, call, cleanup_content
from awgbot.bot.states import GatewayJoinCode
from awgbot.bot.texts.routing import join as jt
from awgbot.domain.services import ServiceError

from ._router import router

log = logging.getLogger("awgbot.handlers.settings")


def _CANCEL():
    """«✖️ Отмена» под приглашением кода — ввод сброшен, на месте приглашения
    «🛰 Шлюзы»; сообщение подключения остаётся как было."""
    return kb.cancel_input("set_rt", 0)


async def _edit_msg(bot, st: dict, text: str, markup=None) -> None:
    msg = st.get("msg") or []
    if len(msg) != 2:
        return
    try:
        await bot.edit_message_text(text, chat_id=int(msg[0]), message_id=int(msg[1]), reply_markup=markup)
    except Exception as e:                                # noqa: BLE001 — удалено, не изменилось
        if "not modified" not in str(e):
            log.info("сопряжение: сообщение подключения не поправлено: %s", e)


async def gwjoin_go(message: Message, services, *, device_id: int | None = None, slot: int = 0,
                    same: bool = False) -> None:
    """Выпустить команду подключения и прислать сообщение подключения."""
    try:
        res = await call(services.gwjoin_issue, device_id=device_id, slot_id=slot or None, same=same)
    except ServiceError as e:
        await message.answer(f"⚠️ {texts._e(str(e))}", reply_markup=kb.settings_back("rt"))
        return
    st = res["state"]
    stale = res.get("stale")
    if stale:
        await _edit_msg(message.bot, {"msg": stale}, jt.JOIN_STALE, kb.gwjoin_final())
    sent = await message.answer(jt.join_message(st, res["command"]),
                                reply_markup=kb.gwjoin_message(st["slot"], st["id"],
                                                               replace=st.get("kind") != "new"))
    await call(services.gwjoin_set_message, st["slot"], sent.chat.id, sent.message_id)
    from awgbot.runtime import linkserver
    try:
        await linkserver.ensure(services)                 # слушатель на адресе линка сопряжения — сразу
    except Exception as e:                                # noqa: BLE001
        log.warning("сопряжение: слушатель канала не поднят: %s", e)


# ── кнопки сообщения подключения ─────────────────────────────────────────────

@router.callback_query(GwJoinCB.filter(F.action == "code"))
async def gwjoin_code_ask(cb: CallbackQuery, callback_data: GwJoinCB, services, state: FSMContext):
    how = await call(services.gwjoin_wait, callback_data.slot, callback_data.jid)
    if how == "wait":
        await cb.answer(ui.toast(jt.JOIN_CODE_WAIT), show_alert=True)
        return
    if how != "ask":
        await cb.answer(ui.toast(jt.JOIN_GONE), show_alert=True)
        return
    await cb.answer()
    await state.set_state(GatewayJoinCode.value)
    await state.update_data(gwjoin_slot=callback_data.slot, gwjoin_id=callback_data.jid)
    await ask_tracked(cb.message, services, jt.JOIN_CODE_PROMPT, reply_markup=_CANCEL())


@router.message(GatewayJoinCode.value)
async def gwjoin_code_received(message: Message, state: FSMContext, services):
    text = message.text or ""
    try:
        await message.delete()                            # ввод админа — из чата прочь
    except Exception:                                     # noqa: BLE001
        pass
    data = await state.get_data()
    slot, jid = int(data.get("gwjoin_slot") or 0), str(data.get("gwjoin_id") or "")
    res = await call(services.gwjoin_code, slot, jid, text)
    from awgbot.runtime import linkserver
    result = res.get("result")
    if result == "format":
        await ask_tracked(message, services, jt.JOIN_CODE_FORMAT, reply_markup=_CANCEL())
        return
    if result == "wrong":
        await linkserver.join_wrong(slot, int(res.get("left") or 0))
        await ask_tracked(message, services, jt.join_code_wrong(int(res.get("left") or 0)), reply_markup=_CANCEL())
        return
    await state.clear()
    await cleanup_content(message.bot, services, message.chat.id)   # приглашение и переспросы — прочь
    st = res.get("st") or {}
    if result == "ok":
        await _edit_msg(message.bot, st, jt.join_accepted(st))
        await linkserver.join_accepted(slot)
        return
    if result == "refused":
        await linkserver.join_close(services, slot, "code")
        await _edit_msg(message.bot, st, jt.join_refused(st), kb.gwjoin_final())
        return
    if result == "expired":
        await linkserver.join_close(services, slot, "expired")      # интерфейс под токен — сразу (Р24)
        await on_join_event(message.bot, services, "expired", st, {})
        return
    if result == "wait":
        await ask_tracked(message, services, jt.JOIN_CODE_WAIT)
        return
    await ask_tracked(message, services, jt.JOIN_GONE)


@router.callback_query(GwJoinCB.filter(F.action == "cancel"))
async def gwjoin_cancel(cb: CallbackQuery, callback_data: GwJoinCB, services):
    st = await call(services.gwjoin, callback_data.slot)
    if not st or st.get("id") != callback_data.jid or st.get("state") not in ("waiting", "connected"):
        await cb.answer(ui.toast(jt.JOIN_GONE), show_alert=True)
        return
    await cb.answer()
    if st.get("kind") in ("replace", "same"):
        try:
            await cb.message.edit_reply_markup(reply_markup=kb.gwjoin_cancel_confirm(st["slot"], st["id"]))
        except Exception:                                 # noqa: BLE001
            pass
        await cb.message.answer(jt.join_cancel_ask(st), reply_markup=kb.gwjoin_cancel_confirm(st["slot"], st["id"]))
        return
    await _cancel(cb, services, callback_data)


@router.callback_query(GwJoinCB.filter(F.action == "cancel_yes"))
async def gwjoin_cancel_yes(cb: CallbackQuery, callback_data: GwJoinCB, services):
    await cb.answer()
    await _cancel(cb, services, callback_data)


@router.callback_query(GwJoinCB.filter(F.action == "cancel_no"))
async def gwjoin_cancel_no(cb: CallbackQuery, callback_data: GwJoinCB, services):
    """Диалог отмены замены закрыт: кнопки сообщения подключения — назад."""
    await cb.answer()
    st = await call(services.gwjoin, callback_data.slot)
    if cb.message.text and cb.message.text.startswith("↩️"):
        try:
            await cb.message.delete()
        except Exception:                                 # noqa: BLE001
            pass
    if st.get("id") == callback_data.jid:
        msg = st.get("msg") or []
        if len(msg) == 2:
            try:
                await cb.bot.edit_message_reply_markup(chat_id=int(msg[0]), message_id=int(msg[1]),
                                                       reply_markup=kb.gwjoin_message(st["slot"], st["id"],
                                                                                      replace=True))
            except Exception:                             # noqa: BLE001
                pass


async def _cancel(cb: CallbackQuery, services, callback_data: GwJoinCB) -> None:
    st = await call(services.gwjoin_cancel, callback_data.slot, callback_data.jid)
    if not st:
        return
    from awgbot.runtime import linkserver
    await linkserver.join_close(services, st["slot"], "cancel")   # интерфейс под токен — сразу (Р24)
    if cb.message.text and cb.message.text.startswith("↩️"):
        try:
            await cb.message.delete()                     # диалог отмены замены отслужил
        except Exception:                                 # noqa: BLE001
            pass
    await _edit_msg(cb.bot, st, jt.join_cancelled(st), kb.gwjoin_final())


# ── события хода сопряжения (крючок слушателя канала) ────────────────────────

async def on_join_event(bot, services, event: str, st: dict, extra: dict) -> None:
    """Шлюз подключился, заново, с двух адресов; срок вышел; готово."""
    if event in ("connected", "reconnected", "two"):
        # токена в записи нет — команда собирается заново из конфигов линка
        cmd = await call(services.gwjoin_command_for, st)
        if not cmd:
            return
        await _edit_msg(bot, st, jt.join_message(st, cmd, event=event),
                        kb.gwjoin_message(st["slot"], st["id"], replace=st.get("kind") != "new"))
        return
    if event == "expired":
        await _edit_msg(bot, st, jt.join_expired(st), kb.gwjoin_final())
        from awgbot.bot.notifier import notify_one
        from awgbot.core import config
        await notify_one(bot, config.ADMIN_ID, jt.join_expired(st), silent=True)
        return
    if event == "done":
        version, bot_name = await call(services.gwjoin_done_info, int(st["slot"]))
        await _edit_msg(bot, st, jt.join_done(st, version, bot_name), kb.gwjoin_final())


def set_hooks(bot, services) -> None:
    from awgbot.runtime import linkserver

    async def _on_join(event: str, st: dict, extra: dict) -> None:
        await on_join_event(bot, services, event, st, extra)
    linkserver.set_on_join(_on_join)


__all__ = ["gwjoin_go", "on_join_event", "set_hooks"]
