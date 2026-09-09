"""
restore.py — восстановление из резервной копии, присланной в чат. Общее для
обеих ролей: осмотр файла (шифр, метка роли и даты ВНУТРИ архива), вопрос с
предупреждением, запуск `awg-bot restore --yes` вне cgroup бота.
"""
from __future__ import annotations

import base64
import io

from aiogram.types import CallbackQuery, Message

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.handlers.common import call

_MAX_BYTES = 64 * 1024 * 1024
_SUFFIXES = (".tgz", ".tgz.enc", ".tar.gz", ".tar.gz.enc")


def looks_like_backup(doc) -> bool:
    name = (getattr(doc, "file_name", "") or "").lower()
    return name.endswith(_SUFFIXES)


async def offer_restore(message: Message, services, state, *, gateway: bool) -> bool:
    """Скачать, осмотреть, задать вопрос. True — файл принят к рассмотрению."""
    doc = message.document
    if doc.file_size and doc.file_size > _MAX_BYTES:
        await message.answer(texts.restore_rejected("файл слишком большой для резервной копии"))
        return True
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    blob = buf.getvalue()
    info = await call(services.inspect_backup, blob, doc.file_name or "")
    if not info.get("ok"):
        await message.answer(texts.restore_rejected(info.get("error", "не удалось прочитать")))
        return True
    await state.update_data(restore_plain=base64.b64encode(info["plain"]).decode(),
                            restore_at=info["created_at"])
    await message.answer(texts.restore_offer(info["created_at"]),
                         reply_markup=kb.restore_confirm(gateway=gateway))
    return True


async def run_restore(cb: CallbackQuery, services, state) -> None:
    data = await state.get_data()
    raw = data.get("restore_plain")
    await state.clear()
    if not raw:
        await cb.answer("Файла в памяти нет — пришли его заново.", show_alert=True)
        return
    await cb.answer()
    path = await call(services.prepare_restore, base64.b64decode(raw))
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                  # noqa: BLE001
        pass
    await cb.message.answer(texts.RESTORE_STARTED)
    await call(services.launch_restore, path)


async def drop_restore(cb: CallbackQuery, state) -> None:
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                  # noqa: BLE001
        pass
    await cb.answer("Файл отброшен")


async def report_restore_result(bot, services) -> None:
    """На старте: маркер от awg-bot restore → отчёт админу."""
    from awgbot.core import config
    from awgbot.bot.notifier import notify_one
    done = await call(services.pop_restore_done)
    if not done:
        return
    await notify_one(bot, config.ADMIN_ID, texts.restore_done(str(done.get("created_at") or "")))
