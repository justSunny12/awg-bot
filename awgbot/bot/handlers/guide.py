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
from awgbot.bot.handlers.common import (call, drop_message, own_device,
                                        send_device_config, show_main_menu, ask_tracked)
from awgbot.domain.services import BYTES_PER_GB, LimitReached, ServiceError
from awgbot.bot.states import AddDeviceGuide

# Скриншоты гайдов лежат в пакете рядом с кодом (переживают деплой как обычный
# ресурс). Путь от этого модуля: awgbot/bot/handlers/ → awgbot/assets/guides/.
_GUIDE_ASSETS = Path(__file__).resolve().parents[1].parent / "assets" / "guides"

router = Router(name="guide")
router.message.filter(RoleFilter("client"))
router.callback_query.filter(RoleFilter("client"))

_PLATFORM_GUIDE = {"apple": "apple", "android": "android",
                   "windows": "windows", "mac": "mac"}


async def _render(cb: CallbackQuery, services, client, guide: str, step: int):
    """Единый рендер шага гайда. Одно сообщение = один экран: если у шага есть
    скриншот — фото с подписью и кнопками, иначе текст с кнопками. Telegram не
    даёт менять тип сообщения (текст↔фото) редактированием, поэтому при смене
    типа старое сообщение удаляем и шлём новое, обновив nav_message_id."""
    last = guides.step_count(guide) - 1
    text = guides.step_text(guide, step)

    if guides.base_guide(guide) == "connect" and step == 0:
        devices = await call(services.db.list_devices, client.id)
        slots = await call(services.device_slots, client.id)
        await _render_screen(cb, services, text, None,
                             kb.guide_connect_devices(devices, slots, last, guide=guide))
        return

    next_guide = guides.NEXT_GUIDE.get(guide) if step == last else None
    apple_connect_end = (guides.is_apple_connect(guide) and step == last)
    markup = kb.guide_nav(guide, step, last, next_guide=next_guide,
                          apple_connect_end=apple_connect_end)
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
    """Показать шаг настройки (step 1) с выбором способа подключения
    (ссылка/QR/файл) для выбранного устройства. Артефакт выдаёт выбранная кнопка
    (guide_connect_deliver), затем ведёт на шаг 2 «Подключаемся»."""
    variant = guide if guides.base_guide(guide) == "connect" else "connect"
    await message.answer(guides.step_text(variant, 1),
                         reply_markup=kb.guide_connect_method(dev.id, variant))


@router.callback_query(GuideCB.filter(F.kind.in_(("link", "qr", "file"))))
async def guide_connect_deliver(cb: CallbackQuery, callback_data: GuideCB, services, client):
    """Шаг 1 → выдать артефакт выбранным способом и показать шаг 2 «Подключаемся»
    новым сообщением ПОД выданным конфигом (порядок как раньше при авто-выдаче)."""
    dev = await call(own_device, services, client, callback_data.dev)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    await drop_message(cb)                       # убрать сообщение с выбором способа
    try:
        await send_device_config(cb.message, services, dev, callback_data.kind)
    except ServiceError as e:
        await cb.message.answer(f"Не удалось выдать конфиг: {e}")
        await cb.answer()
        return
    variant = callback_data.guide
    await cb.message.answer(
        guides.step_text(variant, 2),
        reply_markup=kb.guide_connect_done(variant, dev.id,
                                           apple_end=guides.is_apple_connect(variant)))
    await cb.answer()


@router.callback_query(GuideCB.filter(
    F.guide.in_(("connect", "connect_apple")) & (F.step == 1) & (F.kind == "") & (F.dev > 0)))
async def guide_connect_methods(cb: CallbackQuery, callback_data: GuideCB, services, client):
    """Возврат к выбору способа (кнопка «Назад» на шаге 2) — для того же
    устройства. Шаг 1 текстовый, картинки нет → простой edit_text."""
    dev = await call(own_device, services, client, callback_data.dev)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    try:
        await cb.message.edit_text(
            guides.step_text(callback_data.guide, 1),
            reply_markup=kb.guide_connect_method(dev.id, callback_data.guide))
    except TelegramBadRequest:
        await cb.message.answer(
            guides.step_text(callback_data.guide, 1),
            reply_markup=kb.guide_connect_method(dev.id, callback_data.guide))
    await cb.answer()


# ── запуск гайда из меню помощи ──────────────────────────────────────────────

@router.callback_query(HelpCB.filter(F.platform.in_(_PLATFORM_GUIDE)))
async def help_launch(cb: CallbackQuery, callback_data: HelpCB, services, client):
    await _render(cb, services, client, _PLATFORM_GUIDE[callback_data.platform], 0)
    await cb.answer()


# ── выбор существующего устройства на шаге 0 подключения ─────────────────────

@router.callback_query(DeviceCB.filter(F.action == "gen_guide"))
async def guide_pick_device(cb: CallbackQuery, callback_data: DeviceCB, services, client):
    dev = await call(own_device, services, client, callback_data.device_id)
    if dev is None:
        await cb.answer("Устройство не найдено", show_alert=True)
        return
    # удалить сообщение-список, чтобы кнопки не висели над ссылкой
    await drop_message(cb)
    await _deliver_and_advance(cb.message, services, client, dev, "connect")
    await cb.answer()


# ── добавление устройства внутри гайда ───────────────────────────────────────

@router.callback_query(GuideCB.filter(F.step == -1))
async def guide_add_device(cb: CallbackQuery, callback_data: GuideCB, services, client, state: FSMContext):
    used, limit = await call(services.device_slots, client.id)
    if limit != 0 and used >= limit:              # 0 = безлимит
        await cb.answer("Лимит устройств исчерпан", show_alert=True)
        return
    await state.set_state(AddDeviceGuide.name)
    await state.update_data(return_guide=callback_data.guide)
    await ask_tracked(cb.message, services, "Введи имя нового устройства:", reply_markup=kb.reply_cancel())
    await cb.answer()


@router.message(AddDeviceGuide.name, RoleFilter("client"))
async def guide_add_device_name(message: Message, services, client, state: FSMContext):
    name = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not name:
        await ask_tracked(message, services, "Имя не может быть пустым. Введи ещё раз:")
        return
    await state.update_data(dev_name=name)
    await state.set_state(AddDeviceGuide.traffic)
    await ask_tracked(message, services, texts.traffic_limit_device_ask(int(client.traffic_limit)),
                      reply_markup=kb.reply_cancel())


@router.message(AddDeviceGuide.traffic, RoleFilter("client"))
async def guide_add_device_traffic(message: Message, services, client, state: FSMContext):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit():
        await ask_tracked(message, services, texts.TRAFFIC_LIMIT_BAD)
        return
    data = await state.get_data()
    name = data.get("dev_name")
    return_guide = data.get("return_guide", "connect")
    tlimit = int(raw) * BYTES_PER_GB
    await state.clear()
    try:
        created = await call(services.add_device, client.id, name, tlimit)
    except (LimitReached, ServiceError) as e:
        await message.answer(f"Не удалось создать устройство: {e}", reply_markup=kb.reply_hide())
        await show_main_menu(message, services, "client", client)
        return
    dev_count = len(await call(services.db.list_devices, client.id))
    plimit = await call(services.profile_traffic_limit, client.id)
    await message.answer(
        texts.device_created_report(name, device_count=dev_count,
                                    max_devices=client.device_limit,
                                    dev_limit_bytes=tlimit, profile_limit_bytes=plimit),
        reply_markup=kb.reply_hide())
    dev = await call(services.db.get_device, created.device_id)
    await _deliver_and_advance(message, services, client, dev, return_guide)


# ── навигация по шагам ───────────────────────────────────────────────────────

@router.callback_query(GuideCB.filter())
async def guide_step(cb: CallbackQuery, callback_data: GuideCB, services, client):
    await _render(cb, services, client, callback_data.guide, callback_data.step)
    await cb.answer()


__all__ = ["router"]
