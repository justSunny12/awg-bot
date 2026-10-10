"""ask.py — вопросы человеку о документе с сервера AWG: «⏳ Решить», ответы «Оставить своё» / «Принять с сервера AWG»."""
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
    await send_menu_to(bot, services, chat_id, texts.cfg_question_text(q), kb.gateway_cfg_question_kb(q["section"]))
    return True


@router.callback_query(GwCB.filter(F.action == "cfg_ask"))
async def gw_cfg_ask(cb: CallbackQuery, services, state: FSMContext):
    """«⏳ Решить» на панели: первый вопрос очереди — правкой панели."""
    await state.clear()
    queue = await call(services.cfg_questions)
    q = await call(services.cfg_question, queue[0]) if queue else None
    if q is None:
        await cb.answer(ui.toast("Решать нечего — вопросов нет"))
        await pkg._panel(cb.message, services, cb)
        return
    await edit_nav(cb, services, texts.cfg_question_text(q), kb.gateway_cfg_question_kb(q["section"]))
    await cb.answer()


@router.callback_query(GwCB.filter(F.action.in_({"cfg_no", "cfg_ok!"})))
async def gw_cfg_answer(cb: CallbackQuery, callback_data: GwCB, services, state: FSMContext):
    """Ответ: «Оставить своё» — памятка по отпечатку серверного значения,
    «Принять с сервера AWG» — применить. Итог — правкой сообщения вопроса;
    ответ серверу — сразу или из очереди; следующий вопрос — новым меню."""
    section = callback_data.val if callback_data.val in ("mail", "backup") else ""
    accept = callback_data.action == "cfg_ok!"
    if not section:
        await cb.answer(ui.toast("Вопрос не найден"))
        return
    if await call(services.cfg_question_stale, section):
        await call(services.cfg_release, section)
        await edit_nav(cb, services, texts.QUESTION_STALE[section], None)
        await cb.answer()
        await _next_or_panel(cb, services)
        return
    answer = await call(services.cfg_answer, section, accept)
    if not answer.get("ok"):
        await cb.answer(ui.toast("Вопрос уже решён"))
        await pkg._panel(cb.message, services, cb)
        return
    try:
        await linkclient.report_cfg_answer(services, answer)
    except Exception as e:                                # noqa: BLE001
        log.warning("gateway: ответ серверу о разделе %s не ушёл: %s", section, e)
    await edit_nav(cb, services, texts.cfg_answer_text(section, accept, answer.get("note")), None)
    await cb.answer()
    await _next_or_panel(cb, services)


async def _next_or_panel(cb: CallbackQuery, services) -> None:
    if not await show_cfg_question(cb.bot, services, cb.message.chat.id):
        await pkg._panel(cb.message, services, keep_id=cb.message.message_id)
