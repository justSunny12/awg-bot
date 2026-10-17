"""
restore.py — восстановление сервера AWG из бэкапа, присланного в чат: осмотр
файла (только шифрованный; метка схемы 2, версия, поколение, админ — ВНУТРИ
архива), вопрос с планом — что вернётся, что останется текущим, что будет со
шлюзами, — запуск `awg-bot restore --yes` вне cgroup бота, отчёт по маркеру и
ожидание выхода шлюзов на связь. Только сервер AWG: у шлюза своих бэкапов нет,
бэкап в его чате убирается с объяснением (handlers/gateway/bundle).
"""
from __future__ import annotations

import base64
import io

from aiogram.types import CallbackQuery, Message

from awgbot.bot import texts, roles
from awgbot.bot import ui
from awgbot.bot.handlers.common import call, edit_nav, forget_secret, send_menu

_MAX_BYTES = 20 * 1024 * 1024          # облачный Bot API отдаёт боту файлы до 20 МБ
_SUFFIXES = (".tgz", ".tgz.enc", ".tar.gz", ".tar.gz.enc")


def looks_like_backup(doc) -> bool:
    name = (getattr(doc, "file_name", "") or "").lower()
    return name.endswith(_SUFFIXES)


async def offer_restore(message: Message, services, state) -> bool:
    """Скачать, осмотреть, задать вопрос с планом. True — файл принят к
    рассмотрению."""
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
    await forget_secret(message)                       # в копии вся база — в чате ей не место
    await state.update_data(restore_blob=base64.b64encode(blob).decode(), restore_at=info["created_at"],
                            restore_plan=info["plan"])
    br = roles.MAIN
    from awgbot.bot.sections import backup as _backup
    from awgbot.domain.backup.plan import Plan
    # вопрос — живым меню: прежнее гаснет, «Отмена» вернёт на его место главную
    await send_menu(message, services, texts.restore_offer_plan(Plan.from_dict(info["plan"]),
                                                                getattr(services, "bot_username", "") or ""),
                    _backup.restore_confirm_kb(br))
    return True


async def run_restore(cb: CallbackQuery, services, state) -> None:
    data = await state.get_data()
    raw = data.get("restore_blob")
    await state.clear()
    if not raw:
        await cb.answer(ui.Toast.no_file, show_alert=True)
        return
    await cb.answer()
    path = await call(services.prepare_restore, base64.b64decode(raw), data.get("restore_plan"))
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:                                  # noqa: BLE001
        pass
    await cb.message.answer(texts.RESTORE_STARTED)
    await call(services.launch_restore, path)


async def drop_restore(cb: CallbackQuery, services, state, hooks) -> None:
    """Отказ: на месте вопроса — главный экран роли, одно живое меню в чате."""
    await state.clear()
    await cb.answer(ui.Toast.file_dropped)
    parts = await hooks.main(services, cb.message.chat.id)
    if parts is None:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                              # noqa: BLE001
            pass
        return
    await edit_nav(cb, services, *parts)


async def report_restore_result(bot, services) -> None:
    """На старте: маркер от awg-bot restore → отчёт админу по каждому шлюзу и
    ожидание их выхода на связь (факт — hello слота)."""
    from awgbot.core import config
    from awgbot.bot.notifier import notify_one
    failed = await call(services.pop_restore_failed) if hasattr(services, "pop_restore_failed") else ""
    if failed:
        await notify_one(bot, config.ADMIN_ID, texts.restore_failed(failed))
    done = await call(services.pop_restore_done)
    if not done:
        return
    report = done.get("merge") if isinstance(done.get("merge"), dict) else {}
    await notify_one(bot, config.ADMIN_ID,
                     texts.restore_done_report(str(done.get("created_at") or ""), report,
                                               getattr(services, "bot_username", "") or ""))
    if hasattr(services, "restore_wait_start"):
        await call(services.restore_wait_start, report)
