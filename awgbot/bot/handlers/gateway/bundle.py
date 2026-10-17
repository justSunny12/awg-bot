"""bundle.py — часть пакета gateway (см. __init__): файлы в чате агента."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from awgbot.bot import texts
from awgbot.bot.handlers.common import forget_secret
from awgbot.util import bundlecrypt
from .panel import router
from awgbot.bot.handlers import gateway as pkg       # имена пакета — через него: их подменяют тесты


# ── файл в чате ──────────────────────────────────────────────────────────────

@router.message(F.document)
async def gw_bundle_document(message: Message, services, state: FSMContext):
    """Конфигурацию агент получает только документом по каналу конфигурации,
    своих бэкапов у шлюза нет: бэкап в чате (шлюза прежних версий или сервера)
    и файл конфигурации шлюза — любой: первого применения или шифрованный
    перевыпуск прежних выпусков, по имени или по началу — один ответ и убрать
    из чата (внутри ключи), прочие файлы — молча игнорировать: отвечать «не
    примем» незачем, раз конфигурацию тоже не принимаем (решения пользователя
    17.10.2026)."""
    doc = message.document
    from awgbot.domain.backup.inspect import recognize
    name = (getattr(doc, "file_name", "") or "").lower()
    kind = recognize(name)
    if kind:                                           # бэкап по имени: не скачиваем, не осматриваем
        await _backup_refused(message, kind)
        return
    if name.startswith("awg-gw-bundle") and name.endswith(".sh"):
        await forget_secret(message)
        await message.answer(texts.GW_REISSUE_FILE_REFUSED)
        return
    if doc.file_size and doc.file_size > pkg._BUNDLE_MAX_BYTES:
        return                                         # крупнее предела Bot API — заведомо не наш

    import io
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    head = buf.getvalue()[:4096]
    if head.startswith(bundlecrypt.MAGIC) or (head.startswith(b"#!/bin/sh") and b"awg-gw-bundle" in head):
        # файл конфигурации шлюза под любым именем — что внутри, не важно
        await forget_secret(message)
        await message.answer(texts.GW_REISSUE_FILE_REFUSED)
        return
    kind = recognize(name, buf.getvalue())
    if kind:                                           # бэкап под чужим именем — по содержимому
        await _backup_refused(message, kind)
        return
    # прочий файл (в том числе архив выпуска awg-bot.tgz) — не наш: молча


async def _backup_refused(message: Message, kind: str) -> None:
    """Бэкап в чате шлюза — один ответ, файл из чата прочь (в нём ключи)."""
    await forget_secret(message)
    await message.answer(texts.GW_OLD_BACKUP_REFUSED if kind == "gw" else texts.GW_SERVER_BACKUP_REFUSED)


# Ввод значения, парольная фраза и мастер почты регистрирует роутер общих
