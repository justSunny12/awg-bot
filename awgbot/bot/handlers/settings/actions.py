"""actions.py — переезд профилей, восстановление, шифрование, почта, общий do."""

from __future__ import annotations

import logging
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.bot import texts
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers import settingscore as core
from awgbot.bot.handlers.common import call, edit, send_menu

log = logging.getLogger("awgbot.handlers.settings")
from ._router import router
from .inputs import _firewall_action, _migration_prepare, _routing_provision
from .render import HOOKS, _record, _render, _screen, send_gw_bundle

# ── действия (бэкап сейчас, рестарты, проверка обновлений) ────────────────────
# ── переезд профилей (docs/ROADMAP.md, п.3) ──────────────────────────────────
@router.callback_query(SetCB.filter((F.sec == "mig") & (F.act == "do")))
async def migration_action(cb: CallbackQuery, callback_data: SetCB, services):
    """Рычаг переезда и оба выхода.

    Ключ с восклицательным знаком — подтверждённое действие. Через
    подтверждение проходят все три: завершение и отмена необратимы по-разному,
    а старт меняет то, что получит КАЖДЫЙ следующий попросивший конфиг. Все
    трое обязаны показать последствия до нажатия, а не после.

    Итог каждого из трёх остаётся в чате отдельным сообщением (_record): экран
    настроек переписывается следующей навигацией, а «когда начали» и «чем
    кончилось» спрашивают потом.
    """
    key = callback_data.key
    if not await call(services.migration_available):
        await cb.answer("Переезд не настроен: пустые ключи в app.yaml", show_alert=True)
        return
    # Сторож состояния. Колбэк приходит и из СТАРОГО сообщения в истории чата
    # (тот же класс, что у раздела маршрутизации): «finish!» с прошлогоднего
    # подтверждения, нажатый после отмены, снёс бы старые пиры орфанов и
    # заархивировал ровно то, что отмена сохранила.
    running = await call(services.migration_running)
    if key in ("pending", "finish", "cancel", "finish!", "cancel!") and not running:
        await cb.answer("Переезд сейчас не идёт — экран устарел", show_alert=True)
        return
    if key in ("start", "start!") and running:
        # зеркальная половина сторожа: «start!» со старого подтверждения,
        # нажатый уже во время переезда, пересобрал бы выдачу вслепую
        await cb.answer("Переезд уже идёт — экран устарел", show_alert=True)
        return

    if key == "start":
        clients, devices, to_birth = await call(services.migration_start_preview)
        await edit(cb, texts.migration_start_confirm(clients, devices, to_birth),
                   kb.migration_confirm("start"))
        await cb.answer()
        return

    if key == "start!":
        await cb.answer("Создаю новые профили…")
        res = await call(services.migration_start)
        await _record(cb, texts.migration_started(res), services)
        return

    if key == "pending":
        rows = await call(services.migration_pending)
        await edit(cb, texts.migration_pending_text(rows), kb.settings_back("svc"))
        await cb.answer()
        return

    if key == "orphans":
        rows = await call(services.migration_orphan_rows)    # имена одним проходом
        await edit(cb, texts.migration_orphans_text(rows), kb.settings_back("svc"))
        await cb.answer()
        return

    if key == "finish":
        _, dropped = await call(services.migration_finish_preview)
        await edit(cb, texts.migration_finish_confirm(dropped),
                   kb.migration_confirm("finish"))
        await cb.answer()
        return

    if key == "cancel":
        moved = len(await call(services.migration_moved_devices))
        await edit(cb, texts.migration_cancel_confirm(moved),
                   kb.migration_confirm("cancel"))
        await cb.answer()
        return

    if key == "finish!":
        await cb.answer("Завершаю…")
        removed, dropped, failed = await call(services.migration_finish)
        await _record(cb, texts.migration_finished(removed, dropped, failed), services)
        if not failed:
            # шлюзы получили двойников с новыми ключами — файлы сразу, по слоту
            for g in await call(services.db.gateways):
                await send_gw_bundle(cb.message, services, g.id)
        # Смена поколения могла ждать финала этого переезда (установщик её не
        # начинал, чтобы не подменить цель). Теперь очередь дошла.
        if not failed:
            from awgbot.infra import awglock
            if awglock.needs_migration() and not await call(services.migration_available):
                await cb.message.answer(texts.migration_generation_pending(),
                                        reply_markup=kb.migration_generation_pending())
        promoted = await call(services.pop_promoted_iface)
        if promoted:
            # Новый интерфейс стал основным. Деплой-значения читаются при старте,
            # поэтому рестарт здесь не косметика: без него бот продолжит считать
            # основным погашенный интерфейс и родит следующее устройство на нём.
            dns = (await call(services.private_dns_info))
            await send_menu(cb.message, services, texts.migration_promoted(
                promoted, dns["dns1"] if dns["mode"] == "private" else ""), kb.restart_now_or_later())
        return

    if key == "cancel!":
        await cb.answer("Отменяю…")
        moved = await call(services.migration_cancel)
        await _record(cb, texts.migration_cancelled(moved), services)
        return

    await cb.answer("Действие недоступно", show_alert=True)

# ── ♻️ Восстановление из файла в чате ────────────────────────────────────────
@router.callback_query(SetCB.filter((F.sec == "backup") & (F.act == "do") & (F.key.in_({"restore!", "restore_drop"}))))
async def backup_restore_action(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    from awgbot.bot.handlers import restore as rs
    if callback_data.key == "restore!":
        await rs.run_restore(cb, services, state)
    else:
        await rs.drop_restore(cb, state)


# ── 🔐 Шифрование бэкапов: фраза дважды (шаги — в settingscore) ─────────────
@router.callback_query(SetCB.filter((F.sec == "backup") & (F.act == "do") & (F.key == "enc_set")))
async def backup_passphrase_start(cb: CallbackQuery, state: FSMContext, services):
    await core.passphrase_start(cb, services, HOOKS, state)


# ── ✉️ E-mail: мастер подключения, проверка, отключение ─────────────────────
@router.callback_query(SetCB.filter((F.sec == "email") & (F.act == "do")))
async def email_action(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    if not await core.email_action(cb, services, HOOKS, state, callback_data.key):
        await cb.answer("Действие недоступно", show_alert=True)


# Ввод значения, парольная фраза и мастер почты — общие обработчики сообщений.
_core = core.register(router, HOOKS, default_sec="root")
receive_value, backup_passphrase_first, backup_passphrase_second = (
    _core["receive_value"], _core["passphrase_first"], _core["passphrase_second"])
email_address, email_imap_host, email_imap_port = _core["address"], _core["imap_host"], _core["imap_port"]
email_smtp_host, email_smtp_port, email_password = _core["smtp_host"], _core["smtp_port"], _core["password"]


# ВЫШЕ do_action НАМЕРЕННО. Фильтры проверяются в порядке регистрации, а у
# do_action он широкий (F.act == "do") и перехватил бы sec="mig" целиком:
# ключ не подошёл бы ни к одной его ветке, функция закончилась бы молча —
# без ответа на колбэк, то есть с вечным спиннером на кнопке. По той же
# причине выше стоит и routing_action.
@router.callback_query(SetCB.filter(F.act == "do"))
async def do_action(cb: CallbackQuery, callback_data: SetCB, services):
    key = callback_data.key
    if callback_data.sec == "fw":
        await _firewall_action(cb, callback_data, services)
        return
    if callback_data.sec == "rt" and key == "provision":
        await _routing_provision(cb, services)
        return
    if callback_data.sec == "mig_prep" and key == "go":
        await _migration_prepare(cb, services, callback_data.val)
        return
    if key == "enc":                                   # экран шифрования
        mode = await call(services.backup_encryption_mode)
        await edit(cb, texts.backup_encryption_text(mode), kb.backup_encryption_kb(bool(mode)))
        await cb.answer()
        return
    if key == "now":                                   # бэкап сейчас
        await core.backup_now(cb, services, HOOKS)
        return
    if key in ("awg", "bot"):                          # сначала — цена действия
        await edit(cb, texts.SVC_CONFIRM_AWG if key == "awg" else texts.SVC_CONFIRM_BOT,
                   kb.svc_confirm(key))
        await cb.answer()
        return
    if key == "awg!":                                  # рестарт AWG — итог первой строкой раздела
        await cb.answer("Перезапускаю AWG…")           # ответ сразу: рестарт может идти долго
        try:
            await call(services.restart_service)
            note = texts.SVC_AWG_RESTARTED
        except Exception as e:                         # noqa: BLE001
            note = f"⚠️ Ошибка перезапуска AWG: {texts._e(str(e))}"
        from awgbot.bot import screens
        text, markup = await _screen("svc", services)
        await edit(cb, screens.with_note(text, note), markup)
        return
    if key == "bot!":                                  # рестарт бота
        await cb.answer("Перезапускаю бота…")
        await edit(cb, "🔁 Бот перезапускается — вернётся через несколько секунд", None)
        # Запоминаем ДО рестарта: обещание вернуться исполняет новый процесс,
        # подменяя это же сообщение панелью.
        await call(services.set_restart_wait, cb.message.chat.id, cb.message.message_id)
        await call(services.restart_bot)
        return
    if key == "check":                                 # старая кнопка: раздел проверяет сам
        await cb.answer("Проверяю…")
        await _render(cb, "upd", services)
        return
