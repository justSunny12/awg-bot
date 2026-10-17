"""bundle.py — часть пакета gateway (см. __init__): файлы в чате агента."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.handlers.common import call, forget_secret
from awgbot.util import bundlecrypt
from .panel import router, log
from awgbot.bot.handlers import gateway as pkg       # имена пакета — через него: их подменяют тесты


# ── файл в чате ──────────────────────────────────────────────────────────────

@router.message(F.document)
async def gw_bundle_document(message: Message, services, state: FSMContext):
    """Конфигурацию агент получает только документом по каналу конфигурации,
    своих бэкапов у шлюза нет. Бэкап шлюза прежних версий и файл конфигурации
    шлюза (первого применения или шифрованный перевыпуск прежних выпусков, по
    имени или по началу) — один ответ и убрать из чата (внутри ключи). Бэкап
    сервера AWG — серверу по каналу: вторая точка входа восстановления, вопрос
    — в чате бота сервера. Прочие файлы — молча игнорировать (решения
    пользователя 17.10.2026)."""
    doc = message.document
    from awgbot.domain.backup.inspect import recognize
    name = (getattr(doc, "file_name", "") or "").lower()
    kind = recognize(name)
    if kind == "gw":                                   # бэкап шлюза прежних версий: не скачиваем
        await _backup_refused(message)
        return
    if name.startswith("awg-gw-bundle") and name.endswith(".sh"):
        await forget_secret(message)
        await message.answer(texts.GW_REISSUE_FILE_REFUSED)
        return
    if doc.file_size and doc.file_size > pkg._BUNDLE_MAX_BYTES:
        if kind:                                       # бэкап сервера по имени, но Bot API его не отдаст
            await forget_secret(message)
            sent = await message.answer(texts.GW_RELAY_SENDING)
            await _say(message, sent, texts.gw_relay_result({"ok": False, "why": "big"}, {}))
        return                                         # крупнее предела Bot API — заведомо не наш

    import io
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    data = buf.getvalue()
    head = data[:4096]
    if head.startswith(bundlecrypt.MAGIC) or (head.startswith(b"#!/bin/sh") and b"awg-gw-bundle" in head):
        # файл конфигурации шлюза под любым именем — что внутри, не важно
        await forget_secret(message)
        await message.answer(texts.GW_REISSUE_FILE_REFUSED)
        return
    kind = recognize(name, data)
    if kind == "gw":
        await _backup_refused(message)
        return
    if kind:                                           # бэкап сервера — серверу по каналу
        await _relay(message, services, data, name)
        return
    # прочий файл (в том числе архив выпуска awg-bot.tgz) — не наш: молча


async def _backup_refused(message: Message) -> None:
    """Бэкап шлюза прежних версий — один ответ, файл из чата прочь (в нём ключи)."""
    await forget_secret(message)
    await message.answer(texts.GW_OLD_BACKUP_REFUSED)


async def _relay(message: Message, services, blob: bytes, name: str) -> None:
    """Бэкап сервера AWG в чате бота шлюза — вторая точка входа восстановления:
    файл из чата прочь (в нём ключи), серверу по каналу, итог — правкой
    «⏳ Передаю…». Сервер дальше — штатные рельсы (вопрос в чате бота сервера)."""
    from awgbot.core import config
    from awgbot.runtime import linkclient
    if getattr(getattr(message, "from_user", None), "id", None) != config.ADMIN_ID:
        return                                         # только от админа: отправитель не известен — нет
    await forget_secret(message)
    if not linkclient.relay_reserve():                 # одна передача за раз: второй файл ждёт итога первого
        await message.answer(texts.GW_RELAY_BUSY, reply_markup=kb.hide_only())
        return
    try:
        sent = await message.answer(texts.GW_RELAY_SENDING)
        try:
            res = await linkclient.relay_backup(blob, name)
        except Exception as e:                         # noqa: BLE001
            log.warning("gateway: бэкап серверу не передан: %s", e)
            res = {"ok": False, "why": "broken", "error": ""}
    finally:
        linkclient.relay_release()
    # блоб — только в памяти этого вызова: на диск шлюза он не попадал и уйдёт вместе с ним
    await _say(message, sent, texts.gw_relay_result(res, await call(services.link_server_bot)))


async def _say(message: Message, sent, text: str) -> None:
    """Итог — правкой «⏳ Передаю…» с «Скрыть»; сообщение удалили — новым."""
    try:
        await sent.edit_text(text, reply_markup=kb.hide_only())
    except Exception:                                  # noqa: BLE001 — сообщение удалено: итог новым
        await message.answer(text, reply_markup=kb.hide_only())


# Ввод значения, парольная фраза и мастер почты регистрирует роутер общих
