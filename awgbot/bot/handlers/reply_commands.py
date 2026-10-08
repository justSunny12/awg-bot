"""
handlers/reply_commands.py — кнопки действий на уведомлениях (NoteCB),
«✖️ Отмена» под приглашением к вводу (инлайн, CancelCB) и reply-кнопка
«✖️ Отмена» у поля ввода (старый образец — живёт одну версию для чатов, где
ввод был открыт в момент обновления). «Скрыть» — handlers/hide.

Инлайн-«Отмена» нужна обеим ролям — фабрика make_cancel_router(), свой Router
на диспетчер (bot/routers.py, до общих разделов). Модульный router (NoteCB и
reply-«Отмена») включает только основной бот — первым из своих роутеров, до
общих разделов настроек (bot/sections): reply-«Отмена» ловится по точному
тексту + StateFilter("*") раньше FSM-хендлеров (иначе записалась бы как
значение или имя).
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import CancelCB, NoteCB
from awgbot.bot.handlers.common import call, edit_nav, show_main_menu

router = Router(name="reply_commands")


async def on_cancel_inline(cb: CallbackQuery, callback_data: CancelCB, state: FSMContext,
                           services, role: str = "", client=None):
    """«✖️ Отмена» под приглашением к вводу: диалог сброшен, экран-контекст —
    на месте приглашения, без сообщения-следа. Роль — из middleware, экран —
    из реестра; нет экрана — главная роли."""
    from awgbot.bot import screens
    await state.clear()
    parts = await screens.render(callback_data.kind, callback_data.ref, services=services,
                                 role=role, client=client, chat_id=cb.message.chat.id)
    if parts is None:
        parts = await screens.render("main", services=services, role=role, client=client,
                                     chat_id=cb.message.chat.id)
    await cb.answer()
    if parts is None:
        return
    await edit_nav(cb, services, *parts)
    # приглашение снова стало экраном: из служебных долой, иначе уборка при
    # возврате в меню снесёт живое меню
    await call(services.db.remove_content_msg_id, cb.message.chat.id, cb.message.message_id)


def make_cancel_router() -> Router:
    """«✖️ Отмена» под приглашением к вводу — у обеих ролей; свой Router на
    каждый диспетчер."""
    r = Router(name="cancel")
    r.callback_query(CancelCB.filter())(on_cancel_inline)
    return r


@router.callback_query(NoteCB.filter())
async def on_note_action(cb: CallbackQuery, callback_data: NoteCB, state: FSMContext,
                         services, role: str = "", client=None):
    """Кнопка действия на уведомлении: кнопки снимаются (текст остаётся в
    истории), экран приходит новым живым меню. Роль — из middleware; чужая
    подсказка (клиентская у админа и наоборот) — главная роли."""
    from awgbot.bot import screens
    from awgbot.bot.handlers.common import send_menu, cleanup_content
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                 # noqa: BLE001
        pass
    kind, ref = callback_data.kind, callback_data.ref
    if role == "admin" and kind == "gwcfg":
        # перевыпуск конфигурации слота — файл сразу, как из карточки
        from awgbot.bot.handlers.settings import send_gw_bundle
        await cb.answer("Собираю и шифрую…")
        await send_gw_bundle(cb.message, services, ref)
        return
    screen_kind = {"extend": "extend", "unassigned": "unassigned", "sub": "sub"}.get(kind, "main")
    if role != "admin" and screen_kind in ("extend", "unassigned"):
        screen_kind = "main"
    if role == "admin" and screen_kind == "sub":
        screen_kind = "main"
    parts = await screens.render(screen_kind, ref, services=services, role=role, client=client,
                                 chat_id=cb.message.chat.id)
    if parts is None:
        parts = await screens.render("main", services=services, role=role, client=client,
                                     chat_id=cb.message.chat.id)
    await cb.answer()
    if parts is None:
        return
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    await send_menu(cb.message, services, *parts)


@router.message(F.text == kb.BTN_CANCEL, StateFilter("*"))
async def on_cancel(message: Message, state: FSMContext, services,
                    role: str = "", client=None):
    """✖️ Отмена — прервать текстовый диалог в любом состоянии. Финишер называет,
    что именно отменено (по группе состояний), и несёт снятие reply-клавы
    (чтобы «Отмена» не висела); затем — главное меню роли."""
    from awgbot.bot import texts
    what = texts.cancelled(await state.get_state())
    await state.clear()
    await message.answer(what, reply_markup=kb.reply_hide())
    await show_main_menu(message, services, role, client)


__all__ = ["router", "make_cancel_router", "on_cancel_inline"]
