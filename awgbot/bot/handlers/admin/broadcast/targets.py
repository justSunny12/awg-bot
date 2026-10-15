"""targets.py — часть пакета broadcast (см. __init__): роутер, кулдаун, выбор адресатов, тумблер продления, дни."""
from __future__ import annotations

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot.callbacks import BroadcastCB, PresetCB
from awgbot.bot.handlers.common import edit, ask_tracked, cleanup_content, send_menu
from awgbot.bot.states import Broadcast
from awgbot.bot.handlers.admin import broadcast as pkg       # имена пакета — через него: их подменяют тесты


router = Router(name="admin.broadcast")


# ── Броадкаст объявлений ─────────────────────────────────────────────────────
# Вход РОВНО один — с главной админа. Кнопка в карточке профиля существовала и
# убрана: она отвечала на вопрос «кому», который теперь задаётся явным шагом, и
# при этом лезла в глаза там, где адмиn занимается совсем другим.
#
# Набор отмеченных профилей живёт в FSM-data: между выбором и подтверждением
# стоит сообщение пользователя, и восстановить выбор из колбэка на том шаге
# неоткуда. Плюс лимит Telegram — 64 байта на callback_data.
_BROADCAST_COOLDOWN_SEC = 30          # анти-дабл-тап: не слать одно и то же чаще
# Кулдаун ПО АУДИТОРИИ, а не общий. Общий запрещал бы объявить одно и то же
# двум разным наборам подряд — а это не двойное нажатие, а обычная работа.
_last_broadcast_at: dict[tuple, float] = {}


async def _bc_clients(services, extend: bool = False):
    """Профили-кандидаты. Служебный и админский исключены: у первого нет
    владельца, второй — сам отправитель. В режиме с продлением — только
    активировавшие доступ: остальным доставлять некуда."""
    clients = await pkg.call(services.db.list_clients, exclude_tg=config.ADMIN_ID)
    return [c for c in clients if c.tg_id] if extend else clients


async def _bc_selected(services, data: dict) -> list:
    """Отмеченные профили — свежими объектами из БД, в порядке списка."""
    sel = set(data.get("targets") or ())
    # только те, кто получит: профиль без tg (приглашение не принято) в превью
    # и отчёте назван не будет, а в число адресатов и так не входит
    return [c for c in await _bc_clients(services, bool(data.get("extend"))) if c.id in sel and c.tg_id]


async def _bc_show_targets(cb: CallbackQuery, state: FSMContext, services):
    data = await state.get_data()
    extend = bool(data.get("extend"))
    clients = await _bc_clients(services, extend)
    selected = set(data.get("targets") or ())
    from awgbot.bot import paging
    await edit(cb, texts.broadcast_targets_text(len(selected), extend),
               kb.broadcast_targets(clients, selected, extend=extend,
                                    page=paging.page_of(cb.message.chat.id, "bcast")))


@router.callback_query(BroadcastCB.filter(F.action == "pick"))
async def broadcast_pick(cb: CallbackQuery, state: FSMContext, services):
    """Единственный вход в рассылку — сразу адресаты; режим «с продлением» —
    тумблером на том же экране. Черновик — с чистого листа."""
    await state.set_state(Broadcast.targets)
    await state.set_data({"targets": [], "extend": False})
    await _bc_show_targets(cb, state, services)
    await cb.answer()


@router.callback_query(BroadcastCB.filter(F.action == "mode"))
async def broadcast_mode(cb: CallbackQuery, callback_data: BroadcastCB, state: FSMContext,
                         services):
    """Кнопка старого образца — тот же экран адресатов."""
    await state.set_state(Broadcast.targets)
    await state.set_data({"targets": [], "extend": bool(callback_data.ref)})
    await _bc_show_targets(cb, state, services)
    await cb.answer()


@router.callback_query(BroadcastCB.filter(F.action == "ext"))
async def broadcast_extend_toggle(cb: CallbackQuery, state: FSMContext, services):
    """Тумблер «С продлением подписки»: с продлением получают только
    активировавшие профили — не активировавшие снимаются с отметки."""
    data = await state.get_data()
    extend = not bool(data.get("extend"))
    if extend:
        ids = {c.id for c in await _bc_clients(services, True)}
        await state.update_data(targets=sorted(set(data.get("targets") or ()) & ids))
    await state.update_data(extend=extend)
    await _bc_show_targets(cb, state, services)
    await cb.answer()


@router.callback_query(BroadcastCB.filter(F.action == "targets"))
async def broadcast_targets_again(cb: CallbackQuery, state: FSMContext, services):
    """Перерисовать выбор адресатов как есть — по нему листаются страницы."""
    await _bc_show_targets(cb, state, services)
    await cb.answer()


@router.callback_query(BroadcastCB.filter(F.action == "tgl"))
async def broadcast_toggle(cb: CallbackQuery, callback_data: BroadcastCB,
                           state: FSMContext, services):
    sel = set((await state.get_data()).get("targets") or ())
    sel.symmetric_difference_update({callback_data.ref})
    await state.update_data(targets=sorted(sel))
    await _bc_show_targets(cb, state, services)
    await cb.answer()


@router.callback_query(BroadcastCB.filter(F.action == "all"))
async def broadcast_toggle_all(cb: CallbackQuery, state: FSMContext, services):
    """Отметить всех либо снять всех. Направление выводим из состояния: когда
    отмечено уже всё, осмысленно только снять."""
    data = await state.get_data()
    ids = [c.id for c in await _bc_clients(services, bool(data.get("extend")))]
    sel = set(data.get("targets") or ())
    # ✅ (все отмечены, в том числе по одному) — снять все; ☑️ — довести до всех
    await state.update_data(targets=[] if ids and sel.issuperset(ids) else ids)
    await _bc_show_targets(cb, state, services)
    await cb.answer()


@router.callback_query(BroadcastCB.filter(F.action == "next"))
async def broadcast_next(cb: CallbackQuery, state: FSMContext, services):
    data = await state.get_data()
    clients = await _bc_selected(services, data)
    if not clients:
        await cb.answer(texts.BROADCAST_NO_TARGETS, show_alert=True)
        return
    if data.get("extend"):
        # шаг дней. Бессрочным продлевать нечего: отмечены только они —
        # выбран не тот режим, а не «продлить на ноль»
        plan = await pkg.call(services.extension_plan, [c.id for c in clients], 1)
        if all(e.unlimited for e in plan):
            await cb.answer(texts.BROADCAST_ALL_UNLIMITED, show_alert=True)
            return
        await state.set_state(Broadcast.days)
        await edit(cb, texts.broadcast_days_prompt(plan, getattr(services, "bot_username", "") or ""),
                   kb.broadcast_days_kb())
        await cb.answer()
        return
    friends = await pkg.call(services.db.broadcast_has_friends, {c.id for c in clients},
                         config.ADMIN_ID)
    await state.set_state(Broadcast.text)
    await edit(cb, texts.broadcast_prompt(clients, friends, bot_username=getattr(services, "bot_username", "") or ""),
               kb.broadcast_cancel())
    await cb.answer()


async def _days_chosen(message: Message, state: FSMContext, services, days: int,
                       via_cb: CallbackQuery | None = None) -> None:
    data = await state.get_data()
    clients = await _bc_selected(services, data)
    await state.update_data(days=days)
    await state.set_state(Broadcast.text)
    bot = getattr(services, "bot_username", "") or ""
    text = texts.broadcast_prompt(clients, False, extend_days=days, bot_username=bot)
    if via_cb is not None:
        await edit(via_cb, text, kb.broadcast_cancel())
        await via_cb.answer()
        return
    nav = await pkg.call(services.db.get_nav_message_id, message.chat.id)
    if nav:
        await pkg.call(services.db.add_content_msg_id, message.chat.id, nav)
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, text, kb.broadcast_cancel())


@router.callback_query(PresetCB.filter(F.kind == "bc_days"), Broadcast.days)
async def broadcast_days_preset(cb: CallbackQuery, callback_data: PresetCB, state: FSMContext, services):
    if callback_data.val < 0:
        # приглашение — на месте, «✖️ Отмена» — та же отмена объявления, что у
        # остальных шагов (bc:cancel), а не возврат на главную
        await edit(cb, texts.BROADCAST_DAYS_OTHER, kb.broadcast_cancel())
        await pkg.call(services.db.add_content_msg_id, cb.message.chat.id, cb.message.message_id)
        await cb.answer()
        return
    await _days_chosen(cb.message, state, services, int(callback_data.val), via_cb=cb)


@router.message(Broadcast.days)
async def broadcast_days(message: Message, state: FSMContext, services):
    """Своё число дней («✏️ Другое»)."""
    await pkg.call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 365:
        await ask_tracked(message, services, texts.BROADCAST_DAYS_BAD, reply_markup=kb.broadcast_cancel())
        return
    await _days_chosen(message, state, services, int(raw))
