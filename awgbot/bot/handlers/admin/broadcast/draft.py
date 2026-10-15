"""draft.py — часть пакета broadcast (см. __init__): приём черновика, замки по чату, отложенное превью."""
from __future__ import annotations

import asyncio
from awgbot.core import config
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from awgbot.bot.handlers.common import ask_tracked
from awgbot.bot.notifier import send_announcement
from awgbot.bot.states import Broadcast
from .targets import router, _bc_selected
from awgbot.bot.handlers.admin import broadcast as pkg       # имена пакета — через него: их подменяют тесты


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
_bc_locks: dict[tuple[int, int], asyncio.Lock] = {}   # (чат, цикл событий) → замок: Lock привязан к циклу


def _bc_lock(chat_id: int) -> asyncio.Lock:
    key = (int(chat_id), id(asyncio.get_running_loop()))
    lock = _bc_locks.get(key)
    if lock is None:
        lock = _bc_locks[key] = asyncio.Lock()
    return lock
_bc_render_tasks: dict[int, asyncio.Task] = {}


def _bc_cancel_render(chat_id: int) -> None:
    task = _bc_render_tasks.pop(chat_id, None)
    if task:
        task.cancel()


async def _bc_render_later(message: Message, state: FSMContext, services):
    await asyncio.sleep(pkg._BC_SETTLE_SECONDS)
    await pkg._bc_preview(message, state, services)


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
    lock = _bc_lock(chat_id)
    async with lock:
        # Своё сообщение админа — в уборку: иначе после отмены оно остаётся
        # висеть, а вместе с ним и весь набранный черновик объявления. Под
        # замком: список id читается и пишется целиком, и параллельные апдейты
        # альбома теряли друг друга — снимки оставались в чате.
        await pkg.call(services.db.add_content_msg_id, message.chat.id, message.message_id)
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
        await pkg._bc_preview(message, state, services)


async def _bc_preview(message: Message, state: FSMContext, services):
    """Единая точка превью: собрать черновик, проверить, показать.

    Превью ПЕРЕСОБИРАЕТСЯ на каждом изменении черновика: прежнее удаляется,
    новое встаёт следом. Иначе в чате копились бы альбомы, неотличимые от
    разосланного, и по два живых блока подтверждения.
    """
    chat_id = message.chat.id
    lock = _bc_lock(chat_id)
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
            await pkg.call(services.db.add_content_msg_id, chat_id, note.message_id)
            await state.update_data(preview_ids=[note.message_id])
            return
        sel = set(data.get("targets") or ())
        tg_ids = await pkg.call(services.db.broadcast_recipients_for_clients,
                            sel, config.ADMIN_ID, owners_only=extend)
        if not tg_ids:
            await state.clear()
            await message.answer("Некому отправлять — нет активных получателей.")
            return
        clients = await _bc_selected(services, data)
        friends = (False if extend
                   else await pkg.call(services.db.broadcast_has_friends, sel, config.ADMIN_ID))
        # С продлением: в превью — шапка с масками вместо чисел (у каждого
        # адресата они свои), в подвале — все даты.
        extension = None
        shown = text
        if extend:
            days = int(data.get("days") or 0)
            plan = await pkg.call(services.extension_plan, sel, days)
            extension = (days, plan)
            shown = texts.announcement_text(texts.EXTENSION_HEADER_MASK, text)
        # Экран-приглашение тоже в уборку. Сам он не исчезнет: при переходе на
        # превью навигация лишь СНИМАЕТ с него кнопки (_dismiss_previous_nav), и
        # текст «пришли объявление» остаётся висеть над перепиской.
        nav = await pkg.call(services.db.get_nav_message_id, chat_id)
        if nav:
            await pkg.call(services.db.add_content_msg_id, chat_id, nav)
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
                    len(tg_ids), clients, friends, has_text=bool(text), extension=extension,
                    bot_username=getattr(services, "bot_username", "") or "")
                if notes:
                    confirm_text = notes[0] + "\n\n" + confirm_text
                confirm = await message.answer(confirm_text,
                                               reply_markup=kb.broadcast_confirm())
                await state.update_data(preview_ids=[*ids, confirm.message_id])
            else:
                confirm_text = texts.broadcast_preview(shown, len(tg_ids), clients, friends,
                                                       extension, bot_username=getattr(services, "bot_username", "") or "")
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
