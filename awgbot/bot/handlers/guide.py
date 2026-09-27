"""
handlers/guide.py — роутер визарда-гайдов.

Состояние (какой гайд, какой шаг) целиком в GuideCB — переживает рестарт бота.
Шаг 0 гайда «connect» интерактивный: кнопки устройств/добавления. Выбор или
создание устройства ведёт на шаг 1 «Настраиваем подключение» с выбором способа
(ссылка/QR/файл); по выбору бот выдаёт артефакт и показывает шаг 2 «Подключаемся»
(поднятие туннеля). Отдельной «Далее» на шаге 0 нет — навигацию несут кнопки.
"""

from __future__ import annotations

from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message

from awgbot.bot import guides
from awgbot.bot import texts
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import DeviceCB, GuideCB, HelpCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import (call, drop_message, own_device, held_device,
                                        send_device_config, ask_here, ask_tracked,
                                        back_to_context)
from awgbot.domain.services import LimitReached, ServiceError
from awgbot.bot.states import AddDeviceGuide

# Скриншоты гайдов лежат в пакете рядом с кодом (переживают деплой как обычный
# ресурс). Путь от этого модуля: awgbot/bot/handlers/ → awgbot/assets/guides/.
_GUIDE_ASSETS = Path(__file__).resolve().parents[1].parent / "assets" / "guides"

router = Router(name="guide")
# те же пошаговые гайды — клиенту и гостю; добавить устройство может только клиент
router.message.filter(RoleFilter("client", "invited"))
router.callback_query.filter(RoleFilter("client", "invited"))


# экран реестра «guide»: ref — вариант шага подключения (отмена ввода имени
# внутри гайда возвращает на тот же шаг, а не на главную)
GUIDE_REF = {"connect": 0, "connect_apple": 1}
GUIDE_BY_REF = {v: k for k, v in GUIDE_REF.items()}


async def connect_step0_payload(services, client, ref: int = 0, chat_id: int = 0):
    """(текст, клавиатура) шага 0 подключения — для реестра экранов."""
    guide = GUIDE_BY_REF.get(int(ref or 0), "connect")
    guest = _guest(client)
    if guest:
        devices = await call(services.db.list_held_devices, client.id)
        slots = (len(devices), 0)
    else:
        devices = await call(services.db.list_devices, client.id)
        slots = await call(services.device_slots, client.id)
    from awgbot.bot import paging
    return (guides.step_text(guide, 0),
            kb.guide_connect_devices(devices, slots, guide=guide, guest=guest,
                                     page=paging.page_of(chat_id or client.tg_id, "guidedev")))


def _guest(client) -> bool:
    return bool(getattr(client, "is_guest", False))


def _device(services, client, device_id: int):
    """Устройство, которым человек владеет (клиент) или держит (гость)."""
    if _guest(client):
        return held_device(services, client, device_id)
    return own_device(services, client, device_id)

_PLATFORM_GUIDE = {"apple": "apple", "android": "android",
                   "windows": "windows", "mac": "mac"}


async def _render(cb: CallbackQuery, services, client, guide: str, step: int):
    """Единый рендер шага гайда. Одно сообщение = один экран: если у шага есть
    скриншот — фото с подписью и кнопками, иначе текст с кнопками. Telegram не
    даёт менять тип сообщения (текст↔фото) редактированием, поэтому при смене
    типа старое сообщение удаляем и шлём новое, обновив nav_message_id."""
    last = guides.step_count(guide) - 1
    text = guides.step_text(guide, step)

    guest = _guest(client)
    if guides.base_guide(guide) == "connect" and step == 0:
        if guest:
            devices = await call(services.db.list_held_devices, client.id)
            slots = (len(devices), 0)
        else:
            devices = await call(services.db.list_devices, client.id)
            slots = await call(services.device_slots, client.id)
        from awgbot.bot import paging
        await _render_screen(cb, services, text, None,
                             kb.guide_connect_devices(devices, slots, guide=guide, guest=guest,
                                                      page=paging.page_of(cb.message.chat.id, "guidedev")))
        return

    next_guide = guides.NEXT_GUIDE.get(guide) if step == last else None
    apple_connect_end = (guides.is_apple_connect(guide) and step == last)
    markup = kb.guide_nav(guide, step, last, next_guide=next_guide,
                          apple_connect_end=apple_connect_end, guest=guest)
    img = guides.step_image(guide, step)
    path = (_GUIDE_ASSETS / img) if img else None
    await _render_screen(cb, services, text, path if (path and path.exists()) else None, markup)


async def _render_screen(cb: CallbackQuery, services, text: str, image_path, markup) -> None:
    """Показать экран шага одним сообщением. Редактирует текущее нав-сообщение,
    если тип совпадает; при смене типа (текст↔фото) пересоздаёт его."""
    msg = cb.message
    has_photo = bool(getattr(msg, "photo", None))  # текущее сообщение — фото?
    want_photo = image_path is not None
    try:
        if want_photo and has_photo:
            await msg.edit_media(
                InputMediaPhoto(media=FSInputFile(image_path), caption=text),
                reply_markup=markup)
            return
        if not want_photo and not has_photo:
            await msg.edit_text(text, reply_markup=markup)
            return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        # прочее — упадём в пересоздание ниже
    # смена типа (или правка не удалась) → удаляем старое, шлём новое нужного типа
    try:
        await msg.delete()
    except Exception:                              # noqa: BLE001
        pass
    if want_photo:
        sent = await msg.answer_photo(FSInputFile(image_path), caption=text, reply_markup=markup)
    else:
        sent = await msg.answer(text, reply_markup=markup)
    await call(services.db.set_nav_message_id, sent.chat.id, sent.message_id)


async def _deliver_and_advance(message: Message, services, client, dev, guide: str):
    """Показать шаг настройки (step 1) с рядом выдачи для выбранного
    устройства. Содержимое выдаёт выбранная кнопка (guide_connect_deliver),
    затем шаг 2 «Подключаемся»."""
    variant = guide if guides.base_guide(guide) == "connect" else "connect"
    sent = await message.answer(guides.step_text(variant, 1),
                                reply_markup=kb.guide_connect_method(dev.id, variant,
                                                                     guest=_guest(client)))
    await call(services.db.nav_touch, sent.chat.id, sent.message_id)


@router.callback_query(GuideCB.filter(F.kind.in_(("link", "qr", "file"))))
async def guide_connect_deliver(cb: CallbackQuery, callback_data: GuideCB, services, client):
    """Шаг 1 → выдать артефакт выбранным способом и показать шаг 2 «Подключаемся»
    новым сообщением ПОД выданным конфигом (порядок как раньше при авто-выдаче)."""
    dev = await call(_device, services, client, callback_data.dev)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await drop_message(cb)                       # убрать сообщение с выбором способа
    try:
        await send_device_config(cb.message, services, dev, callback_data.kind)
    except ServiceError as e:
        await cb.message.answer(f"Не удалось выдать конфиг: {texts._e(str(e))}")
        await cb.answer()
        return
    variant = callback_data.guide
    sent = await cb.message.answer(
        guides.step_text(variant, 2),
        reply_markup=kb.guide_connect_done(variant, dev.id, guest=_guest(client),
                                           apple_end=guides.is_apple_connect(variant)))
    await call(services.db.nav_touch, sent.chat.id, sent.message_id)
    await cb.answer()


@router.callback_query(GuideCB.filter(
    F.guide.in_(("connect", "connect_apple")) & (F.step == 1) & (F.kind == "") & (F.dev > 0)))
async def guide_connect_methods(cb: CallbackQuery, callback_data: GuideCB, services, client):
    """Возврат к выбору способа (кнопка «Назад» на шаге 2) — для того же
    устройства. Шаг 1 текстовый, картинки нет → простой edit_text."""
    dev = await call(_device, services, client, callback_data.dev)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    markup = kb.guide_connect_method(dev.id, callback_data.guide, guest=_guest(client))
    try:
        await cb.message.edit_text(guides.step_text(callback_data.guide, 1), reply_markup=markup)
    except TelegramBadRequest:
        await cb.message.answer(guides.step_text(callback_data.guide, 1), reply_markup=markup)
    await cb.answer()


# ── запуск гайда из меню помощи ──────────────────────────────────────────────

@router.callback_query(HelpCB.filter(F.platform.in_(_PLATFORM_GUIDE)))
async def help_launch(cb: CallbackQuery, callback_data: HelpCB, services, client):
    await _render(cb, services, client, _PLATFORM_GUIDE[callback_data.platform], 0)
    await cb.answer()


# ── выбор существующего устройства на шаге 0 подключения ─────────────────────

@router.callback_query(DeviceCB.filter(F.action == "gen_guide"))
async def guide_pick_device(cb: CallbackQuery, callback_data: DeviceCB, services, client):
    dev = await call(_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    # удалить сообщение-список, чтобы кнопки не висели над ссылкой
    await drop_message(cb)
    await _deliver_and_advance(cb.message, services, client, dev, "connect")
    await cb.answer()


# ── добавление устройства внутри гайда ───────────────────────────────────────

@router.callback_query(GuideCB.filter(F.step == -1), RoleFilter("client"))
async def guide_add_device(cb: CallbackQuery, callback_data: GuideCB, services, client, state: FSMContext):
    """Новое устройство внутри гайда: только имя (лимит — в карточке, В5),
    приглашение на месте шага."""
    used, limit = await call(services.device_slots, client.id)
    if limit != 0 and used >= limit:              # 0 = безлимит
        await cb.answer(texts.limit_exhausted_line(used, limit), show_alert=True)
        return
    await state.set_state(AddDeviceGuide.name)
    await ask_here(cb, services, state, texts.add_device_prompt(used, limit, for_friend=False),
                   "guide", GUIDE_REF.get(callback_data.guide, 0), return_guide=callback_data.guide)
    await cb.answer()


@router.message(AddDeviceGuide.name, RoleFilter("client"))
async def guide_add_device_name(message: Message, services, client, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, texts.NAME_EMPTY)
        return
    data = await state.get_data()
    return_guide = data.get("return_guide", "connect")
    await state.clear()
    try:
        created = await call(services.add_device, client.id, name, 0)
    except (LimitReached, ServiceError) as e:
        await back_to_context(message, services, {}, "client", client, note=f"⚠️ {texts._e(str(e))}")
        return
    plimit = await call(services.profile_traffic_limit, client.id)
    from awgbot.bot.handlers.common import cleanup_content
    await cleanup_content(message.bot, services, message.chat.id)
    await message.answer(texts.device_created(name, plimit))
    dev = await call(services.db.get_device, created.device_id)
    await _deliver_and_advance(message, services, client, dev, return_guide)


# ── навигация по шагам ───────────────────────────────────────────────────────

@router.callback_query(GuideCB.filter())
async def guide_step(cb: CallbackQuery, callback_data: GuideCB, services, client):
    await _render(cb, services, client, callback_data.guide, callback_data.step)
    await cb.answer()


__all__ = ["router"]
