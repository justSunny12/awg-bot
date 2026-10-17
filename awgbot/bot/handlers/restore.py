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


def _fingerprint(blob: bytes) -> str:
    import hashlib
    return hashlib.sha256(blob).hexdigest()[:12]


async def offer_restore(message: Message, services, state) -> bool:
    """Скачать, осмотреть, задать вопрос с планом. Бэкап, который не годится,
    убирается из чата (в нём ключи); файл, не похожий на бэкап, остаётся.
    True — файл принят к рассмотрению."""
    from awgbot.domain.backup import inspect as _ins
    doc = message.document
    name = doc.file_name or ""
    if doc.file_size and doc.file_size > _MAX_BYTES:
        if _ins.recognize(name):
            await forget_secret(message)
        await message.answer(texts.restore_rejected(_ins.TOO_BIG))
        return True
    buf = io.BytesIO()
    await message.bot.download(doc, destination=buf)
    blob = buf.getvalue()
    if not _ins.recognize(name, blob):
        await message.answer(texts.restore_rejected(_ins.NOT_BACKUP))
        return True
    await forget_secret(message)                       # в бэкапе вся база — в чате ему не место
    info = await call(services.inspect_backup, blob, name)
    if not info.get("ok"):
        await message.answer(texts.restore_rejected(info.get("error", "не удалось прочитать")))
        return True
    fp = _fingerprint(blob)
    await state.update_data(restore_blob=base64.b64encode(blob).decode(), restore_at=info["created_at"],
                            restore_plan=info["plan"], restore_fp=fp)
    br = roles.MAIN
    from awgbot.bot.sections import backup as _backup
    from awgbot.domain.backup.plan import Plan
    # вопрос — живым меню: прежнее гаснет, «Отмена» вернёт на его место главную
    await send_menu(message, services, texts.restore_offer_plan(Plan.from_dict(info["plan"]),
                                                                getattr(services, "bot_username", "") or ""),
                    _backup.restore_confirm_kb(br, fp))
    return True


async def run_restore(cb: CallbackQuery, services, state, fp: str = "") -> None:
    """«♻️ Восстановить»: кнопка — того самого файла (отпечаток в кнопке и в
    данных вопроса); повтор, пока идёт подготовка, — «уже восстанавливаю»."""
    if getattr(services, "_restore_running", False):     # запущено в этом процессе
        await cb.answer(ui.toast(texts.RESTORE_ALREADY), show_alert=True)
        return
    data = await state.get_data()
    raw = data.get("restore_blob")
    if not raw:
        await cb.answer(ui.Toast.no_file, show_alert=True)
        return
    if fp and data.get("restore_fp") and fp != data["restore_fp"]:
        await cb.answer(ui.toast(texts.RESTORE_OTHER_FILE), show_alert=True)   # вопрос о прежнем файле
        return
    await state.clear()
    services._restore_running = True
    await cb.answer()
    try:
        path = await call(services.prepare_restore, base64.b64decode(raw), data.get("restore_plan"))
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                              # noqa: BLE001
            pass
        await cb.message.answer(texts.RESTORE_STARTED)
        await call(services.launch_restore, path)
    except Exception:
        services._restore_running = False             # не запустилось — кнопку можно нажать снова
        raise


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
    if done:
        report = done.get("merge") if isinstance(done.get("merge"), dict) else {}
        await notify_one(bot, config.ADMIN_ID,
                         texts.restore_done_report(str(done.get("created_at") or ""), report,
                                                   getattr(services, "bot_username", "") or ""))
        if hasattr(services, "restore_wait_start"):
            await call(services.restore_wait_start, report)
    if failed or done:
        # как после обычного перезапуска: под отчётом — живое меню
        try:
            from awgbot.bot.handlers.admin.panel import _panel_parts
            from awgbot.bot.handlers.common import send_menu_to
            await send_menu_to(bot, services, config.ADMIN_ID, *await _panel_parts(services))
        except Exception as e:                         # noqa: BLE001
            import logging
            logging.getLogger("awgbot.restore").warning("меню после восстановления не показано: %s", e)
