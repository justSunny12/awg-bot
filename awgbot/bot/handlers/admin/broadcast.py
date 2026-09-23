"""
handlers/admin/broadcast.py — рассылка объявлений, в том числе режим с
продлением подписки.
"""

from __future__ import annotations

import asyncio
import time

from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from awgbot.bot.callbacks import BroadcastCB
from awgbot.bot.handlers.common import call, edit, edit_nav, ask_tracked, cleanup_content, send_menu
from awgbot.bot.notifier import send_notifications, broadcast, send_announcement
from awgbot.bot.states import Broadcast
from awgbot.bot.handlers.admin.panel import _main_menu_markup, _panel_parts

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
    clients = await call(services.db.list_clients, exclude_tg=config.ADMIN_ID)
    return [c for c in clients if c.tg_id] if extend else clients


async def _bc_selected(services, data: dict) -> list:
    """Отмеченные профили — свежими объектами из БД, в порядке списка."""
    sel = set(data.get("targets") or ())
    return [c for c in await _bc_clients(services, bool(data.get("extend"))) if c.id in sel]


async def _bc_show_targets(cb: CallbackQuery, state: FSMContext, services):
    data = await state.get_data()
    extend = bool(data.get("extend"))
    clients = await _bc_clients(services, extend)
    selected = set(data.get("targets") or ())
    from awgbot.bot import paging
    await edit(cb, texts.BROADCAST_TARGETS_EXTEND if extend else texts.BROADCAST_TARGETS,
               kb.broadcast_targets(clients, selected, extend=extend,
                                    page=paging.page_of(cb.message.chat.id, "bcast")))


@router.callback_query(BroadcastCB.filter(F.action == "pick"))
async def broadcast_pick(cb: CallbackQuery, state: FSMContext, services):
    """Единственный вход в рассылку — выбор режима: простое или с продлением
    подписки. Черновик — с чистого листа."""
    await state.set_state(Broadcast.targets)
    await state.set_data({})
    await edit(cb, texts.BROADCAST_MODE, kb.broadcast_mode())
    await cb.answer()


@router.callback_query(BroadcastCB.filter(F.action == "mode"))
async def broadcast_mode(cb: CallbackQuery, callback_data: BroadcastCB, state: FSMContext,
                         services):
    await state.set_state(Broadcast.targets)
    await state.set_data({"targets": [], "extend": bool(callback_data.ref)})
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
        plan = await call(services.extension_plan, [c.id for c in clients], 1)
        if all(e.unlimited for e in plan):
            await cb.answer(texts.BROADCAST_ALL_UNLIMITED, show_alert=True)
            return
        await state.set_state(Broadcast.days)
        await edit(cb, texts.broadcast_days_prompt(plan), kb.broadcast_cancel())
        await cb.answer()
        return
    friends = await call(services.db.broadcast_has_friends, {c.id for c in clients},
                         config.ADMIN_ID)
    await state.set_state(Broadcast.text)
    await edit(cb, texts.broadcast_prompt(clients, friends), kb.broadcast_cancel())
    await cb.answer()


@router.message(Broadcast.days)
async def broadcast_days(message: Message, state: FSMContext, services):
    """Число дней продления. Своё сообщение админа и переспросы — в уборку;
    приглашение к тексту — новым нав-сообщением, вопрос про дни гаснет."""
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 365:
        await ask_tracked(message, services, texts.BROADCAST_DAYS_BAD,
                          reply_markup=kb.broadcast_cancel())
        return
    data = await state.get_data()
    clients = await _bc_selected(services, data)
    await state.update_data(days=int(raw))
    await state.set_state(Broadcast.text)
    nav = await call(services.db.get_nav_message_id, message.chat.id)
    if nav:
        await call(services.db.add_content_msg_id, message.chat.id, nav)
    await send_menu(message, services,
                    texts.broadcast_prompt(clients, False, extend_days=int(raw)),
                    kb.broadcast_cancel())


# Альбом Telegram доставляет НЕСКОЛЬКИМИ апдейтами, по одному на снимок, и
# aiogram обрабатывает их ПАРАЛЛЕЛЬНО (handle_as_tasks по умолчанию). Отсюда
# двое: замок — иначе конкурентные read-modify-write по FSM теряют снимки, — и
# пауза перед превью, чтобы дождаться хвоста пачки и отрендерить ОДИН раз, а не
# десять. Подпись, набранная в окне вложений, приезжает на одном из сообщений
# альбома — черновик подхватывает её с любого.
# Пауза покрывает только рассинхрон ДОСТАВКИ уже отправленного альбома: клиент
# шлёт его одним запросом после загрузки всех файлов, и апдейты приезжают боту
# пачкой сразу — «конца отправки» как растянутого процесса не существует, ждать
# больше нечего. (Долгие паузы здесь были костылём вокруг чужой ошибки: падал
# разбор подписи, терялся её апдейт, и казалось, что альбом «доезжает».)
_BC_SETTLE_SECONDS = 1.5
_bc_locks: dict[int, asyncio.Lock] = {}
_bc_render_tasks: dict[int, asyncio.Task] = {}


def _bc_cancel_render(chat_id: int) -> None:
    task = _bc_render_tasks.pop(chat_id, None)
    if task:
        task.cancel()


async def _bc_render_later(message: Message, state: FSMContext, services):
    await asyncio.sleep(_BC_SETTLE_SECONDS)
    await _bc_preview(message, state, services)


@router.message(Broadcast.text)
async def broadcast_receive(message: Message, state: FSMContext, services):
    chat_id = message.chat.id
    # Ожидающий рендер отменяем дважды, и оба раза не случайно. ЗДЕСЬ, до
    # первого await, гасится таска, тикающая с ПРОШЛОГО апдейта, — иначе она
    # успела бы дорисовать промежуток, пока мы обрабатываем новый кусок. Но для
    # апдейтов ОДНОЙ пачки этого мало: они обрабатываются параллельно, и второй
    # выполняет эту раннюю отмену раньше, чем первый вообще создал таску, —
    # отменять нечего, обе выживают, и превью пересоздаётся по разу на апдейт.
    # Единственность живой таски даёт вторая отмена — атомарная пара
    # cancel+create в самом конце.
    _bc_cancel_render(chat_id)
    # Своё сообщение админа — в уборку: иначе после отмены оно остаётся висеть,
    # а вместе с ним и весь набранный черновик объявления.
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    lock = _bc_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        data = await state.get_data()
        photos: list = list(data.get("photos") or ())
        draft: dict = {}
        if message.photo:
            if len(photos) >= config.TG_ALBUM_MAX:
                draft["photos_dropped"] = int(data.get("photos_dropped") or 0) + 1
            else:
                # последний размер — оригинал: Telegram отдаёт лесенку превью
                photos.append(message.photo[-1].file_id)
                draft["photos"] = photos
        plain = (message.text or message.caption or "").strip()
        if plain:
            # html_text, а НЕ text: форматирование живёт в entities, голая
            # строка уронила бы разметку. Для ПОДПИСИ работает то же поле:
            # html_text сам берёт text or caption (никакого html_caption у
            # aiogram НЕ существует — обращение к нему роняло хендлер на каждом
            # сообщении с подписью, и апдейт терялся целиком, снимок вместе с
            # текстом). Новый текст ЗАМЕНЯЕТ прежний — так «не влезло, пришли
            # заново» и правка опечатки работают сами. Длину меряем по видимым
            # символам: HTML-теги в счёт Telegram не идут.
            body = message.html_text
            draft["text"], draft["text_len"] = body.strip(), len(plain)
        if draft:
            await state.update_data(**draft)
        elif not message.photo:
            await ask_tracked(message, services, texts.BROADCAST_EMPTY,
                              reply_markup=kb.broadcast_cancel())
            return
    # Превью: после картинки — с паузой (пачка альбома доезжает не одним
    # тиком), после голого текста — сразу, ему пачка не свойственна.
    # Cancel и create — ВПЛОТНУЮ, без await между ними: пара атомарна для
    # event loop, и в каком бы порядке ни финишировали конкурентные апдейты
    # пачки, живой остаётся ровно одна таска — последнего финишировавшего.
    if message.photo:
        _bc_cancel_render(chat_id)
        _bc_render_tasks[chat_id] = asyncio.create_task(
            _bc_render_later(message, state, services))
    else:
        _bc_cancel_render(chat_id)
        await _bc_preview(message, state, services)


async def _bc_preview(message: Message, state: FSMContext, services):
    """Единая точка превью: собрать черновик, проверить, показать.

    Превью ПЕРЕСОБИРАЕТСЯ на каждом изменении черновика: прежнее удаляется,
    новое встаёт следом. Иначе в чате копились бы альбомы, неотличимые от
    разосланного, и по два живых блока подтверждения.
    """
    chat_id = message.chat.id
    lock = _bc_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        data = await state.get_data()
        if not data.get("targets"):
            return          # отменили или отправили, пока превью ждало пачку
        photos = list(data.get("photos") or ())
        for mid in data.get("preview_ids") or ():
            try:
                await message.bot.delete_message(chat_id, mid)
            except Exception:                          # noqa: BLE001
                pass                                   # уже удалено/устарело
        await state.update_data(preview_ids=[])
        notes: list[str] = []
        if data.get("photos_dropped"):
            await state.update_data(photos_dropped=0)
            notes.append(texts.broadcast_too_many_photos())
        text = data.get("text") or ""
        if not text and not photos:
            return                # нечему собираться (страховка, receive отбил)
        # Лимит зависит от наличия картинок: с ними текст едет подписью, а у неё
        # потолок вчетверо ниже. Проверка живёт ЗДЕСЬ, на итоговом черновике:
        # картинка, добавленная после законного длинного текста, меняет лимит
        # задним числом, и проверка только на приёме текста это пропустила бы.
        # Отбивка отказа — с кнопкой отмены и в preview_ids: выход в один тап
        # из любого состояния, и никакого накопления при пересборках.
        extend = bool(data.get("extend"))
        limit = ((config.TG_CAPTION_MAX if photos else config.TG_TEXT_MAX)
                 - (texts.extension_reserve() if extend else 0))   # шапка — из лимита
        length = int(data.get("text_len") or len(text)) if text else 0
        if length > limit:
            notes.append(texts.broadcast_too_long(length, limit, bool(photos)))
            note = await message.answer("\n\n".join(notes),
                                        reply_markup=kb.broadcast_cancel())
            await call(services.db.add_content_msg_id, chat_id, note.message_id)
            await state.update_data(preview_ids=[note.message_id])
            return
        sel = set(data.get("targets") or ())
        tg_ids = await call(services.db.broadcast_recipients_for_clients,
                            sel, config.ADMIN_ID, owners_only=extend)
        if not tg_ids:
            await state.clear()
            await message.answer("Некому отправлять — нет активных получателей.")
            return
        clients = await _bc_selected(services, data)
        friends = (False if extend
                   else await call(services.db.broadcast_has_friends, sel, config.ADMIN_ID))
        # С продлением: в превью — шапка с датами первого продлеваемого (у
        # каждого адресата она своя), в подвале — все даты.
        extension = None
        shown = text
        if extend:
            days = int(data.get("days") or 0)
            plan = await call(services.extension_plan, sel, days)
            extension = (days, plan)
            first = next((e for e in plan if not e.unlimited), None)
            shown = texts.announcement_text(texts.extension_header(days, first), text)
        # Экран-приглашение тоже в уборку. Сам он не исчезнет: при переходе на
        # превью навигация лишь СНИМАЕТ с него кнопки (_dismiss_previous_nav), и
        # текст «пришли объявление» остаётся висеть над перепиской.
        nav = await call(services.db.get_nav_message_id, chat_id)
        if nav:
            await call(services.db.add_content_msg_id, chat_id, nav)
        # Битую разметку ловим здесь: если превью (обёрнутое в HTML) не
        # отправилось — тот же текст провалил бы и рассылку.
        try:
            if photos:
                # С картинками превью — САМО объявление, отправленное админу
                # ровно в том виде, в каком уйдёт людям. Пересказать альбом
                # текстом нельзя: «приложено 3 фото» не показывает ни порядка,
                # ни того, как подпись села под картинками. Блок подтверждения
                # идёт следом — reply_markup у альбома не бывает.
                #
                # БЕЗ текста превью строится ТОЧНО ТАК ЖЕ, а не превращается в
                # переспрос: подпись могла ещё не доехать (она едет на одном из
                # апдейтов альбома, и медленный аплоад растягивает их на десятки
                # секунд) — доедет, и превью пересоберётся само. Блок при этом
                # прямо спрашивает про пустой текст, отправить можно как есть.
                sent = await send_announcement(message.bot, chat_id, shown, photos)
                ids = [m.message_id for m in sent] if isinstance(sent, (list, tuple)) \
                    else [sent.message_id]
                confirm_text = texts.broadcast_preview_photos(
                    len(tg_ids), clients, friends, has_text=bool(text), extension=extension)
                if notes:
                    confirm_text = notes[0] + "\n\n" + confirm_text
                confirm = await message.answer(confirm_text,
                                               reply_markup=kb.broadcast_confirm())
                await state.update_data(preview_ids=[*ids, confirm.message_id])
            else:
                confirm_text = texts.broadcast_preview(shown, len(tg_ids), clients, friends,
                                                       extension)
                if notes:
                    confirm_text = notes[0] + "\n\n" + confirm_text
                confirm = await message.answer(confirm_text,
                                               reply_markup=kb.broadcast_confirm())
                await state.update_data(preview_ids=[confirm.message_id])
        except TelegramBadRequest:
            await ask_tracked(
                message, services,
                "⚠️ Разметка бракованная (незакрытый тег?). Проверь текст и пришли "
                "заново.")


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
    await cleanup_content(cb.bot, services, cb.message.chat.id)
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
    await cleanup_content(cb.bot, services, cb.message.chat.id)
    await edit(cb, "📢 Рассылаю объявление…", None)   # и кнопки сняты (markup=None)
    tg_ids = await call(services.db.broadcast_recipients_for_clients,
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
        plan, notes = await call(services.extend_days, sel, days)
        await send_notifications(cb.bot, notes)      # держателям — «доступ вернулся»
        extension = (days, plan)
        by_tg = {e.client.tg_id: texts.announcement_text(texts.extension_header(days, e), text)
                 for e in plan if e.client.tg_id}
        first = next((e for e in plan if not e.unlimited), None)
        shown = texts.announcement_text(texts.extension_header(days, first), text)
    ok, failed = await broadcast(cb.message.bot, tg_ids, text, photos, by_tg)
    clients = await _bc_selected(services, data)
    friends = (False if extend
               else await call(services.db.broadcast_has_friends, sel, config.ADMIN_ID))
    # Отчёт — ТОЛЬКО факт доставки; само объявление остаётся в чате строкой
    # выше как след разосланного (копии у отправителя нет — Telegram показывает
    # ему лишь собственные реплики боту, а не то, что бот разослал другим).
    # С картинками след — альбом-превью, а cb.message — блок подтверждения под
    # ним: он и редактируется в отчёт. Без картинок cb.message — само превью с
    # текстом и хвостом «Отправляем?»: хвост срезаем (остаётся чистый текст
    # объявления), отчёт приходит следом.
    if photos:
        await edit(cb, texts.broadcast_report(clients, friends, ok, failed, extension), None)
    else:
        await edit(cb, shown, None)
        await cb.message.answer(texts.broadcast_report(clients, friends, ok, failed, extension))
    # Панель — СЛЕДУЮЩИМ сообщением, со своим обычным текстом и статусами.
    # Прежде отчёт нёс на себе клавиатуру главного меню: тогда он либо
    # переписывался при следующей навигации, либо оставлял в чате второе живое
    # меню — инвариант «одно активное» держать было нечем.
    await send_menu(cb.message, services, *await _panel_parts(services),
                    keep_id=cb.message.message_id)
