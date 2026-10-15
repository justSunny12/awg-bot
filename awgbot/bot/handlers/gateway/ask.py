"""ask.py — приглашения и вопросы человеку о документе с сервера AWG: «⏳ Ответить (N)», «Применить» / «⬅️ Отмена», «✅ Принять» / «⬅️ Оставить своё»."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from awgbot.bot import keyboards as kb
from awgbot.bot import texts, ui
from awgbot.bot.callbacks import GwCB
from awgbot.bot.handlers.common import call, edit_nav, send_menu_to
from awgbot.runtime import linkclient

from .panel import router, log
from awgbot.bot.handlers import gateway as pkg


async def show_cfg_question(bot, services, chat_id: int) -> bool:
    """Открыть первый вопрос очереди живым меню (новым сообщением); очередь
    пуста — False. Зовётся крючком канала и после ответа на предыдущий."""
    queue = await call(services.cfg_questions)
    if not queue:
        return False
    q = await call(services.cfg_question, queue[0])
    if q is None:
        return False
    msg_id = await send_menu_to(bot, services, chat_id, texts.cfg_question_text(q), kb.gateway_cfg_question_kb(q["section"]))
    if q["section"] == "link" and msg_id:
        await call(services.rekey_mark_msg, chat_id, msg_id)    # итог и отзыв поправят это сообщение
    return True


@router.callback_query(GwCB.filter(F.action == "cfg_ask"))
async def gw_cfg_ask(cb: CallbackQuery, services, state: FSMContext):
    """«⏳ Ответить (N)» на панели: первый вопрос очереди — правкой панели."""
    await state.clear()
    queue = await call(services.cfg_questions)
    q = await call(services.cfg_question, queue[0]) if queue else None
    if q is None:
        await cb.answer(ui.toast(texts.QUESTIONS_NONE))
        await pkg._panel(cb.message, services, cb)
        return
    await edit_nav(cb, services, texts.cfg_question_text(q), kb.gateway_cfg_question_kb(q["section"]))
    if q["section"] == "link":
        await call(services.rekey_mark_msg, cb.message.chat.id, cb.message.message_id)
    await cb.answer()


@router.callback_query(GwCB.filter(F.action.in_({"cfg_no", "cfg_ok!"})))
async def gw_cfg_answer(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    """Ответ: у приглашения — «⬅️ Отмена» / «Применить», у вопроса — «⬅️ Оставить
    своё» (памятка по отпечатку серверного значения) / «✅ Принять».
    Итог — правкой сообщения; ответ серверу — сразу или из очереди; следующее
    в очереди — новым меню."""
    section = callback_data.val if callback_data.val in ("link", "uplink", "mail", "backup") else ""
    accept = callback_data.action == "cfg_ok!"
    if not section:
        await cb.answer(ui.toast("Кнопка устарела — открой раздел заново"))
        return
    if section == "link":
        await _rekey_answer(cb, services, accept)
        return
    if await call(services.cfg_question_stale, section):
        await call(services.cfg_release, section)
        await edit_nav(cb, services, texts.QUESTION_STALE[section], None)
        await cb.answer()
        await _next_or_panel(cb, services)
        return
    if section == "uplink" and accept:
        await _uplink_accept(cb, services)
        return
    answer = await call(services.cfg_answer, section, accept)
    if not answer.get("ok"):
        await cb.answer(ui.toast("Вопрос уже решён"))
        await pkg._panel(cb.message, services, cb)
        return
    await _report(services, section, answer)
    await edit_nav(cb, services, texts.cfg_answer_text(section, accept, answer.get("note")), None)
    await cb.answer()
    await _next_or_panel(cb, services)


async def _report(services, section: str, answer: dict) -> None:
    try:
        await linkclient.report_cfg_answer(services, answer)
    except Exception as e:                                # noqa: BLE001
        log.warning("gateway: ответ серверу о разделе %s не ушёл: %s", section, e)


async def _rekey_answer(cb: CallbackQuery, services, accept: bool) -> None:
    """Ключи линка: «Применить» — согласие серверу (переключение поведёт он,
    итог придёт отдельным сообщением), «Отмена» — переход снят."""
    try:
        reply = await linkclient.report_rekey_answer(services, accept)
    except Exception as e:                                # noqa: BLE001
        log.warning("gateway: ответ серверу о ключах линка не ушёл: %s", e)
        reply = {"state": "ready" if accept else "declined"}
    if not reply:
        await cb.answer(ui.toast("Вопрос уже решён"))
        await pkg._panel(cb.message, services, cb)
        return
    await edit_nav(cb, services, texts.cfg_answer_text("link", accept, None), None)
    await cb.answer()
    await _next_or_panel(cb, services)


async def _uplink_accept(cb: CallbackQuery, services) -> None:
    """Аплинк — дорога бота в Telegram: сначала сказать, что сейчас пропадём,
    потом применять (до минуты пробы); итог — правкой, а не вышло (сессия
    Telegram умерла вместе с прежним аплинком) — новым сообщением."""
    if "uplink" not in await call(services.cfg_questions):
        # как у ключей линка: всплывашка и панель, без правки сообщения тем же текстом
        await cb.answer(ui.toast(texts.QUESTION_ANSWERED))
        await pkg._panel(cb.message, services, cb)
        return
    await cb.answer()
    await _show(cb, services, texts.UPLINK_APPLYING, new_on_fail=False)
    answer = await call(services.cfg_answer, "uplink", True)
    if not answer.get("ok"):
        await _show(cb, services, texts.QUESTION_ANSWERED)
        await _next_or_panel(cb, services)
        return
    if answer.get("busy"):
        # обвязку применяет прогон юнита — ничего не тронуто, вопрос на месте
        q = await call(services.cfg_question, "uplink")
        if q is not None:
            await _show(cb, services, texts.cfg_question_text(q) + "\n" + texts.UPLINK_BUSY,
                        kb.gateway_cfg_question_kb("uplink"))
        return
    await _report(services, "uplink", answer)
    await _show(cb, services, texts.cfg_answer_text("uplink", True, answer.get("note")))
    await _next_or_panel(cb, services)


async def _show(cb: CallbackQuery, services, text: str, markup=None, new_on_fail: bool = True) -> None:
    """Правка сообщения; не вышло — новым сообщением (после смены аплинка
    прежние соединения с Telegram мертвы, правка может упасть по таймауту)."""
    try:
        await edit_nav(cb, services, text, markup)
        return
    except Exception as e:                                # noqa: BLE001
        log.warning("gateway: сообщение не исправлено: %s", e)
    if not new_on_fail:
        return
    try:
        await send_menu_to(cb.bot, services, cb.message.chat.id, text, markup)
    except Exception as e:                                # noqa: BLE001
        log.warning("gateway: итог не показан и новым сообщением: %s", e)


async def _next_or_panel(cb: CallbackQuery, services) -> None:
    try:
        if not await show_cfg_question(cb.bot, services, cb.message.chat.id):
            await pkg._panel(cb.message, services, keep_id=cb.message.message_id)
    except Exception as e:                                # noqa: BLE001
        log.warning("gateway: следующее в очереди не показано: %s", e)
