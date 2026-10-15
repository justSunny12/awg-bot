"""bundle.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

import base64
import io
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.handlers.common import call, send_menu, forget_secret, drop_previous_nav
from awgbot.util import bundlecrypt
from .panel import router
from awgbot.bot.handlers import gateway as pkg       # имена пакета — через него: их подменяют тесты


# ── бандл файлом ─────────────────────────────────────────────────────────────

@router.message(F.document)
async def gw_bundle_document(message: Message, services, state: FSMContext):
    doc = message.document
    from awgbot.bot.handlers import restore as rs
    if rs.looks_like_backup(doc):                     # резервная копия, не конфигурация
        await rs.offer_restore(message, services, state, gateway=True)
        return
    name = (getattr(doc, "file_name", "") or "").lower()
    if name.startswith("awg-gw-bundle") and name.endswith(".sh"):
        # открытый файл первого применения: внутри ключи линка и аплинка,
        # токен агента — в чате ему не место, а применяют его на устройстве
        await forget_secret(message)
        await message.answer(texts.gw_first_run_file(doc.file_name or ""))
        return
    if doc.file_size and doc.file_size > pkg._BUNDLE_MAX_BYTES:
        await message.answer(texts.gw_bundle_not_ours(await call(services.server_cfg1)))
        return
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    blob = buf.getvalue()
    # Формат проверяем ДО предложения применить, расшифровку — только при
    # применении: чужой файл отбивается сразу, а ключ линка читается один раз.
    if not blob.startswith(bundlecrypt.MAGIC):
        if blob.startswith(b"#!/bin/sh") and b"awg-gw-bundle" in blob[:4096]:
            await forget_secret(message)
            await message.answer(texts.gw_first_run_file(doc.file_name or ""))
            return
        await message.answer(texts.gw_bundle_not_ours(await call(services.server_cfg1)))
        return
    # Файл у нас в памяти — в чате ему делать нечего. Основной бот свою копию
    # убирает по «В меню», а пересланная жила в переписке с агентом вечно:
    # внутри ключ линка, токен агента, фраза шифрования копий, пароль почты.
    await forget_secret(message)
    await state.update_data(bundle=base64.b64encode(blob).decode())
    # осмотр до вопроса: изменится ли конфиг линка — от этого зависит, будет
    # ли обрыв и нужно ли о нём предупреждать. Не расшифровался — скажем при
    # применении, там текст ошибки полный.
    info = await call(services.inspect_bundle, blob)
    link_changed = bool(info.get("link_changed", True)) if info.get("ok") else True
    # Вопрос «применить?» — новое живое меню; прежнюю панель удаляем целиком:
    # без кнопок над итогом применения она только занимала бы экран.
    await drop_previous_nav(message.bot, services, message.chat.id)
    await send_menu(message, services,
                    texts.gateway_bundle_received(link_changed, await call(services.carries_traffic)),
                    kb.gateway_bundle_kb())


# Ввод значения, парольная фраза и мастер почты регистрирует роутер общих
