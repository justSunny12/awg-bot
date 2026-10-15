"""send.py — часть пакета broadcast (см. __init__): отмена на любом шаге и отправка с отчётом."""
from __future__ import annotations

import time
from awgbot.core import config
from awgbot.bot import texts
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.bot.callbacks import BroadcastCB
from awgbot.bot.handlers.common import edit, edit_nav, cleanup_content, send_menu
from awgbot.bot.notifier import send_notifications
from awgbot.bot.handlers.admin.panel import _main_menu_markup, _panel_parts
from .targets import router, _BROADCAST_COOLDOWN_SEC, _last_broadcast_at, _bc_selected
from .draft import _bc_cancel_render
from awgbot.bot.handlers.admin import broadcast as pkg       # имена пакета — через него: их подменяют тесты


@router.callback_query(BroadcastCB.filter(F.action == "cancel"))
async def broadcast_cancel_h(cb: CallbackQuery, state: FSMContext, services):
    """Отмена на любом шаге: сбросить состояние и вернуться в главное меню.

    Альбом-превью удаляем явно, а не через уборку контента: он не «служебное
    сообщение», а точная копия объявления, и оставить его после отмены значит
    оставить в чате запись, неотличимую от разосланной.

    Сброс здесь и есть смысл этого хендлера. Раньше отмена вела прямо в меню,
    чей хендлер чистит FSM попутно, — работало, но держалось на побочном эффекте
    соседа. Без сброса передумавший на шаге ввода админ остался бы в состоянии
    Broadcast.text, и следующее его сообщение стало бы черновиком объявления.
    """
    _bc_cancel_render(cb.message.chat.id)
    for mid in (await state.get_data()).get("preview_ids") or ():
        if mid == cb.message.message_id:
            continue          # его редактируем в панель, удалять нельзя
        try:
            await cb.bot.delete_message(cb.message.chat.id, mid)
        except Exception:                              # noqa: BLE001
            pass                                       # уже удалено/устарело
    await state.clear()
    await cleanup_content(cb.bot, services, cb.message.chat.id, keep=cb.message.message_id)
    await edit_nav(cb, services, *await _panel_parts(services))
    await cb.answer()


@router.callback_query(BroadcastCB.filter(F.action == "send"))
async def broadcast_send(cb: CallbackQuery, state: FSMContext, services):
    _bc_cancel_render(cb.message.chat.id)
    data = await state.get_data()
    text = data.get("text") or ""
    photos = tuple(data.get("photos") or ())
    sel = tuple(sorted(data.get("targets") or ()))
    await state.clear()
    # Текст опционален, когда есть картинки: объявление из одних снимков
    # легально, превью прямо спрашивало про пустую подпись.
    if not sel or (not text and not photos):
        await cb.answer("Нечего отправлять.", show_alert=True)
        return
    # Страховка от устаревшей кнопки: превью пересобирается при каждом изменении
    # черновика, но кнопка «Отправить» могла пережить его в истории чата, а
    # картинка, добавленная после текста, меняет лимит задним числом. Уйди это
    # в Telegram — каждый получатель вернул бы Bad Request, и отчёт записал бы
    # их в «заблокировали бота».
    extend = bool(data.get("extend"))
    limit = ((config.TG_CAPTION_MAX if photos else config.TG_TEXT_MAX)
             - (texts.extension_reserve() if extend else 0))
    length = int(data.get("text_len") or len(text)) if text else 0
    if length > limit:
        await cb.answer(f"Не отправлено: {length} символов при лимите {limit} "
                        "(с картинками текст едет подписью). Сократи и пришли "
                        "заново.", show_alert=True)
        return
    now = time.monotonic()
    if now - _last_broadcast_at.get(sel, 0.0) < _BROADCAST_COOLDOWN_SEC:
        await cb.answer("Только что уже отправляли — подожди немного.", show_alert=True)
        return
    _last_broadcast_at[sel] = now                # метку ставим ДО await'ов —
    await cb.answer("Рассылаю…")                 # второе нажатие уже отсечётся
    # Переписку с набором черновика убираем и здесь, а не только при отмене:
    # после отправки она тем более не нужна, а превью само станет отчётом.
    await cleanup_content(cb.bot, services, cb.message.chat.id, keep=cb.message.message_id)
    await edit(cb, "📢 Рассылаю объявление…", None)   # и кнопки сняты (markup=None)
    tg_ids = await pkg.call(services.db.broadcast_recipients_for_clients,
                        sel, config.ADMIN_ID, owners_only=extend)
    if not tg_ids:
        await edit(cb, "Некому отправлять — нет активных получателей.",
                   await _main_menu_markup(services))
        return
    extension = by_tg = None
    shown = text
    if extend:
        # Сначала продлить, потом отправить: человек читает «увеличена» — к
        # этому моменту это уже правда; а недоставка (заблокировал бота)
        # подарка не отменяет — решение принял админ. Шапка у каждого своя.
        days = int(data.get("days") or 0)
        plan, notes = await pkg.call(services.extend_days, sel, days)
        await send_notifications(cb.bot, notes)      # держателям — «доступ вернулся»
        extension = (days, plan)
        by_tg = {e.client.tg_id: texts.announcement_text(texts.extension_header(days, e), text)
                 for e in plan if e.client.tg_id}
        # копия в чате админа — с масками вместо чисел первого адресата
        shown = texts.announcement_text(texts.EXTENSION_HEADER_MASK, text)
    ok, failed = await pkg.broadcast(cb.message.bot, tg_ids, text, photos, by_tg)
    clients = await _bc_selected(services, data)
    friends = (False if extend
               else await pkg.call(services.db.broadcast_has_friends, sel, config.ADMIN_ID))
    # Отчёт — ТОЛЬКО факт доставки; само объявление остаётся в чате строкой
    # выше как след разосланного (копии у отправителя нет — Telegram показывает
    # ему лишь собственные реплики боту, а не то, что бот разослал другим).
    # С картинками след — альбом-превью, а cb.message — блок подтверждения под
    # ним: он и редактируется в отчёт. Без картинок cb.message — само превью с
    # текстом и хвостом «Отправляем?»: хвост срезаем (остаётся чистый текст
    # объявления), отчёт приходит следом.
    bot_u = getattr(services, "bot_username", "") or ""
    if photos:
        await edit(cb, texts.broadcast_report(clients, friends, ok, failed, extension, bot_username=bot_u), None)
    else:
        await edit(cb, shown, None)
        await cb.message.answer(texts.broadcast_report(clients, friends, ok, failed, extension, bot_username=bot_u))
    # Панель — СЛЕДУЮЩИМ сообщением, со своим обычным текстом и статусами.
    # Прежде отчёт нёс на себе клавиатуру главного меню: тогда он либо
    # переписывался при следующей навигации, либо оставлял в чате второе живое
    # меню — инвариант «одно активное» держать было нечем.
    await send_menu(cb.message, services, *await _panel_parts(services),
                    keep_id=cb.message.message_id)
