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
from awgbot.bot.handlers.common import call, edit_nav, forget_secret, send_menu_to
from awgbot.domain.services import ServiceError

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
    await ask_restore(message.bot, services, state, message.chat.id, blob, info)
    return True


async def ask_restore(bot, services, state, chat_id: int, blob: bytes, info: dict, source: str = "") -> None:
    """Вопрос с планом живым меню — одна дорога для файла из чата сервера и для
    бэкапа, переданного из чата бота шлюза (source — строка источника над
    планом). Прежний вопрос — о другом файле: финишер без кнопок, а не живая
    «♻️ Восстановить»."""
    fp = _fingerprint(blob)
    data = await state.get_data()
    prev = data.get("restore_qmid")
    if prev:
        # прежний вопрос — под кат с пометкой «не актуально», единственная кнопка — «Скрыть»
        from awgbot.bot import keyboards as kb
        try:
            await bot.edit_message_text(texts.stale_finisher(texts.RESTORE_REPLACED, str(data.get("restore_qtext") or "")),
                                        chat_id=chat_id, message_id=int(prev), reply_markup=kb.hide_only())
        except Exception:                              # noqa: BLE001 — сообщение удалено или старое
            pass
    from awgbot.bot.sections import backup as _backup
    from awgbot.domain.backup.plan import Plan
    text = texts.restore_offer_plan(Plan.from_dict(info["plan"]), getattr(services, "bot_username", "") or "")
    text = (source + "\n\n" + text) if source else text
    qmid = await send_menu_to(bot, services, chat_id, text, _backup.restore_confirm_kb(roles.MAIN, fp))
    await state.update_data(restore_blob=base64.b64encode(blob).decode(), restore_at=info["created_at"],
                            restore_plan=info["plan"], restore_fp=fp, restore_qmid=qmid, restore_qtext=text)


async def relay_restore(bot, services, state, blob: bytes, name: str, slot_id: int = 0) -> str:
    """Бэкап сервера из чата бота шлюза (собран слушателем канала): осмотр и
    вопрос в чате бота сервера — как у файла, присланного сюда, с источником
    над планом. «» — вопрос задан; иначе причина для чата бота шлюза (сюда при
    отказе не идёт ничего)."""
    from awgbot.core import config
    if getattr(services, "_restore_running", False):
        return texts.RELAY_RESTORING                    # восстановление уже идёт — осматривать незачем
    info = await call(services.inspect_backup, blob, name)   # расшифровка — в потоке, не в цикле событий
    if not info.get("ok"):
        return str(info.get("error") or "не удалось прочитать")
    gw = await call(services.db.gateway, slot_id) if slot_id else None
    source = ""
    if gw is not None:
        from awgbot.domain.notices import backup as nb
        source = texts.relay_source(nb.gw_ref(await call(services._gw_name, gw), gw.label or "", slot=gw.id,
                                              bot_username=getattr(services, "bot_username", "") or ""))
    try:
        await ask_restore(bot, services, state, config.ADMIN_ID, blob, info, source)
    except Exception as e:                             # noqa: BLE001 — Telegram, а не бэкап
        import logging
        logging.getLogger("awgbot.restore").warning("вопрос о восстановлении через шлюз не задан: %s", e)
        return texts.relay_ask_failed(str(e))
    return ""


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
    from awgbot.domain.selfupdate.marks import install_busy
    if await call(install_busy):
        # замок установки занят (обновление, фиксация, откат): restore отказал
        # бы сам и перезапустил бота ради отчёта — отвечаем здесь, вопрос жив
        from awgbot.domain.notices import fleet as nf
        await cb.answer()
        await cb.message.answer(nf.restore_not_launched(texts.RESTORE_INSTALL_BUSY))
        return
    await state.clear()
    services._restore_running = True
    await cb.answer()
    release = ""
    try:
        try:
            # другая версия — выпуск бэкапа с GitHub, до запуска (sha256 = digest метки)
            release = await call(services.prepare_restore_release, data.get("restore_plan"))
        except Exception as e:                         # noqa: BLE001 — сеть, sha256
            services._restore_running = False
            await state.update_data(**data)            # вопрос остаётся живым: кнопку можно нажать снова
            from awgbot.domain.notices import fleet as nf
            await cb.message.answer(nf.restore_not_launched(str(e)))
            return
        path = await call(services.prepare_restore, base64.b64decode(raw), data.get("restore_plan"))
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                              # noqa: BLE001
            pass
        await cb.message.answer(texts.RESTORE_STARTED)
        await call(services.launch_restore, path, release)
    except Exception:
        services._restore_running = False             # не запустилось — кнопку можно нажать снова
        if release:
            import os
            try:
                os.remove(release)                     # скачанный выпуск без запуска — прочь
            except OSError:
                pass
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
    info = await call(services.pop_restore_failed_info) if hasattr(services, "pop_restore_failed_info") else {}
    failed = str(info.get("failed") or "")
    if failed:
        from awgbot.domain.notices import fleet as nf
        text = (nf.restore_version_failed(info["release"], failed, info.get("from") or config.INSTALLED_VERSION)
                if info.get("release") else texts.restore_failed(failed))
        await notify_one(bot, config.ADMIN_ID, text)
    done = await call(services.pop_restore_done)
    if done:
        report = done.get("merge") if isinstance(done.get("merge"), dict) else {}
        version = str(done.get("version") or "")
        if version and hasattr(services, "fleet_after_restore"):
            # выпуск набора — версия бэкапа: шлюзы пойдут следом по hello
            await call(services.fleet_after_restore, version)
        bot_u = getattr(services, "bot_username", "") or ""
        snap = ""
        from awgbot.domain.notices import backup as nb
        lost = report.get("lost") or []
        if len(lost) > nb.LOST_SHOWN:
            # отчёт — уведомление: весь список не вставших — снимком, хвост ссылкой (метку -n ставит отправка)
            from awgbot.bot.lists.snapshot import snap_param
            snap = await snap_param(services, config.ADMIN_ID, "admin", nb.LOST_TITLE, nb.restore_lost_rows(lost, bot_u),
                                    "", True)
        await notify_one(bot, config.ADMIN_ID,
                         texts.restore_done_report(str(done.get("created_at") or ""), report, bot_u, version=version,
                                                   snap=snap))
        if hasattr(services, "restore_wait_start"):
            try:
                await call(services.restore_wait_start, report)
            except ServiceError as e:              # сервер в отъезде — ждать шлюзов не ему; меню ниже всё равно
                import logging
                logging.getLogger("awgbot.restore").info("ожидание шлюзов после восстановления не начато: %s", e)
    if failed or done:
        # как после обычного перезапуска: под отчётом — живое меню
        try:
            from awgbot.bot.handlers.admin.panel import _panel_parts
            from awgbot.bot.handlers.common import send_menu_to
            await send_menu_to(bot, services, config.ADMIN_ID, *await _panel_parts(services))
        except Exception as e:                         # noqa: BLE001
            import logging
            logging.getLogger("awgbot.restore").warning("меню после восстановления не показано: %s", e)
