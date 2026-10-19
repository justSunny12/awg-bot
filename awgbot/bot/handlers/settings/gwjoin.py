"""gwjoin.py — подключение шлюза командой в чате бота сервера AWG: сообщение
подключения (ждём, подключился, с двух адресов), ввод кода (приглашение,
не тот формат, не совпал, попытки кончились, верный), отмена и её
подтверждение у замены, срок, «Не актуально», итог «шлюз подключён».

Сообщение подключения — обычное сообщение, не живое меню: его правит ход
сопряжения, и навигация по боту не должна снимать с него «✏️ Ввести код».
Ввод кода относится только к заявке нажатой кнопки: слот и id
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
        await message.answer(f"⚠️ {str(e) if e.html else texts._e(str(e))}", reply_markup=kb.settings_back("rt"))
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
    st = await _with_ref(services, res.get("st") or {})
    if result == "ok":
        # «⬅️ В меню» — сообщение не тупик (ui-1); итог «шлюз подключён» заменит его сам
        await _edit_msg(message.bot, st, jt.join_accepted(st), kb.gwjoin_final())
        await linkserver.join_accepted(slot)
        return
    if result in ("refused", "expired"):
        err = await linkserver.join_close(services, slot, "code" if result == "refused" else "expired")
        if err:
            await call(services.gwjoin_revert_told, slot, st.get("id") or "")   # сказано — такт не повторит
            await _edit_msg(message.bot, st, jt.join_revert_failed(st, err), kb.gwjoin_final())
        else:
            await on_join_event(message.bot, services, "code" if result == "refused" else "expired", st, {})
        return
    if result == "wait":
        await ask_tracked(message, services, jt.JOIN_CODE_WAIT)
        return
    await ask_tracked(message, services, jt.JOIN_GONE)


async def _with_ref(services, st: dict) -> dict:
    """Запись с именем-ссылкой на карточку слота (если строка слота есть)."""
    if not st:
        return st
    from awgbot.domain.notices.backup import gw_ref
    slot = int(st.get("slot") or 0)
    has_row = await call(services.db.gateway, slot) is not None if slot else False
    ref = gw_ref(str(st.get("name") or ""), "", slot=slot if has_row else 0,
                 bot_username=getattr(services, "bot_username", "") or "")
    return {**st, "ref": ref}


async def _join_text_back(services, st: dict) -> tuple[str, object] | None:
    """Сообщение подключения как есть сейчас (после «⬅️ Отмена» в диалоге отмены)."""
    cmd = await call(services.gwjoin_command_for, st)
    if not cmd:
        return None
    event = "connected" if st.get("state") == "connected" else ""
    return (jt.join_message(st, cmd, event=event),
            kb.gwjoin_message(st["slot"], st["id"], replace=st.get("kind") != "new"))


def _on_join_message(cb: CallbackQuery, st: dict) -> bool:
    msg = st.get("msg") or []
    return len(msg) == 2 and cb.message is not None and int(msg[1]) == cb.message.message_id


@router.callback_query(GwJoinCB.filter(F.action == "cancel"))
async def gwjoin_cancel(cb: CallbackQuery, callback_data: GwJoinCB, services, state: FSMContext):
    st = await call(services.gwjoin, callback_data.slot)
    if not st or st.get("id") != callback_data.jid or st.get("state") not in ("waiting", "connected"):
        await cb.answer(ui.toast(jt.JOIN_GONE), show_alert=True)
        return
    if st.get("kind") in ("replace", "same"):
        await cb.answer()
        # подтверждение — на самом сообщении подключения (ui-2): вопрос «отменить замену?» и
        # «⬅️ Отмена» · «↩️ Отменить замену»; «⬅️ Отмена» вернёт сообщение
        st = await _with_ref(services, st)
        await _edit_msg(cb.bot, st, jt.join_cancel_ask(st), kb.gwjoin_cancel_confirm(st["slot"], st["id"]))
        return
    await _cancel(cb, services, callback_data, state)


@router.callback_query(GwJoinCB.filter(F.action == "cancel_yes"))
async def gwjoin_cancel_yes(cb: CallbackQuery, callback_data: GwJoinCB, services, state: FSMContext):
    await _cancel(cb, services, callback_data, state)


@router.callback_query(GwJoinCB.filter(F.action == "cancel_no"))
async def gwjoin_cancel_no(cb: CallbackQuery, callback_data: GwJoinCB, services):
    """Диалог отмены замены закрыт: сообщение подключения — как было."""
    st = await call(services.gwjoin, callback_data.slot)
    if st.get("id") != callback_data.jid or st.get("state") not in ("waiting", "connected"):
        await cb.answer(ui.toast(jt.JOIN_GONE), show_alert=True)
        return
    await cb.answer()
    back = await _join_text_back(services, st)
    if back is not None:
        await _edit_msg(cb.bot, st, *back)


async def _cancel(cb: CallbackQuery, services, callback_data: GwJoinCB, state: FSMContext) -> None:
    st = await call(services.gwjoin_cancel, callback_data.slot, callback_data.jid)
    if not st:
        await cb.answer(ui.toast(jt.JOIN_GONE), show_alert=True)      # не молчим (ui-2)
        return
    await cb.answer()
    data = await state.get_data()
    if str(data.get("gwjoin_id") or "") == st["id"]:
        # ввод кода к этой заявке был открыт — сброшен вместе с ней (ui-5)
        await state.clear()
        await cleanup_content(cb.bot, services, cb.message.chat.id)
    from awgbot.runtime import linkserver
    err = await linkserver.join_close(services, st["slot"], "cancel")   # интерфейс под токен — сразу, не по сроку
    if err:
        await call(services.gwjoin_revert_told, st["slot"], st["id"])          # сказано — такт не повторит
    st = await _with_ref(services, st)
    text = jt.join_revert_failed(st, err) if err else jt.join_cancelled(st)
    await _edit_msg(cb.bot, st, text, kb.gwjoin_final())
    if not _on_join_message(cb, st):
        # отменили с экрана «Шлюзы» (ui-3): экран — заново, уже без строки
        from .render import gateways_screen
        from awgbot.bot.handlers.common import edit
        await edit(cb, *await gateways_screen(services))


# ── события хода сопряжения (крючок слушателя канала) ────────────────────────

async def on_join_event(bot, services, event: str, st: dict, extra: dict) -> None:
    """Шлюз подключился, заново, с двух адресов; срок вышел; не доведено;
    готово; возврат после неудачного — по причине закрытия."""
    st = await _with_ref(services, st)
    if event in ("connected", "reconnected", "two"):
        if st.get("state") == "accepted":
            # код уже верный (перезапуск сервера, сессия заново) — сообщение не
            # откатывается к команде (ui-6)
            await _edit_msg(bot, st, jt.join_accepted(st), kb.gwjoin_final())
            return
        # токена в записи нет — команда собирается заново из конфигов линка
        cmd = await call(services.gwjoin_command_for, st)
        if not cmd:
            return
        await _edit_msg(bot, st, jt.join_message(st, cmd, event=event),
                        kb.gwjoin_message(st["slot"], st["id"], replace=st.get("kind") != "new"))
        return
    final = {"expired": jt.join_expired, "stuck": jt.join_stuck, "stale": jt.join_stale,
             "cancel": jt.join_cancelled, "code": jt.join_refused}.get(event)
    if final is not None:
        text = final(st)
        await _edit_msg(bot, st, text, kb.gwjoin_final())
        if event in ("expired", "stuck", "stale"):
            from awgbot.bot.notifier import notify_one
            from awgbot.core import config
            await notify_one(bot, config.ADMIN_ID, text, silent=True)
        return
    if event == "revert_failed":
        # такт не вернул линк под токен — честно, один раз; повторы — молча
        text = jt.join_revert_failed(st, str(extra.get("err") or ""))
        await _edit_msg(bot, st, text, kb.gwjoin_final())
        from awgbot.bot.notifier import notify_one
        from awgbot.core import config
        await notify_one(bot, config.ADMIN_ID, text, silent=True)
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
