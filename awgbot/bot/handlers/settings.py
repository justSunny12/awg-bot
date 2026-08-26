"""handlers/settings.py — экран «⚙️ Настройки» (только админ).

Значения хранятся в conf/*.yaml и меняются через settings.set_value → горячо,
без рестарта. Экран перерисовывается после каждого изменения и показывает
актуальные значения. Раздел под RoleFilter("admin"), как остальная админка.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, FSInputFile

from awgbot.core import config
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import SetCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.states import SettingsInput
from awgbot.bot.handlers.common import call, edit

router = Router(name="settings")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))


# ── рендер экранов ───────────────────────────────────────────────────────────
async def _screen(sec: str, services):
    """(text, markup) для раздела sec.

    Корутина, а не обычная функция: разделы «upd» и «rt» ходят в БД и в
    self_check (тот при холодном кэше запускает ip/ipset/iptables). Синхронный
    вызов держал бы event loop на время рисования экрана — а рядом крутятся
    тик живости и polling. Все остальные разделы чисто текстовые, им await
    ничего не стоит.
    """
    if sec == "notify":
        return texts.SETTINGS_NOTIFY, kb.settings_notify()
    if sec == "subs":
        return texts.SETTINGS_SUBS, kb.settings_subs()
    if sec == "mon":
        return texts.SETTINGS_MON, kb.settings_mon()
    if sec == "backup":
        return texts.SETTINGS_BACKUP, kb.settings_backup()
    if sec == "svc":
        state = await call(services.migration_state)
        avail = await call(services.migration_available)
        progress = await call(services.migration_progress) if state else None
        orphans = 0 if state else len(await call(services.migration_orphan_twins))
        return (texts.settings_svc_text(state, progress),
                kb.settings_svc(state, available=avail, orphans=orphans))
    if sec == "upd":
        return texts.SETTINGS_UPD, kb.settings_updates(await call(services.updates_muted))
    if sec == "rt":
        if not config.ROUTING_ENABLED:
            # Кнопку в этом случае не рисуем вовсе, но колбэк приходит и из
            # старого сообщения в истории чата. Открыть раздел, которого нет,
            # значит показать переключатели, ничего не делающие.
            return texts.SETTINGS_ROUTING_ABSENT, kb.settings_back()
        on = settings.get_bool("app.routing.enabled", False)
        # список профилей нужен только при включённой функции — клавиатура его
        # всё равно не покажет, а лишний запрос к БД на каждый рендер ни к чему
        clients = await call(services.routing_grantable_clients) if on else []
        status = await call(services.routing_status)
        return texts.settings_routing_text(on, status), kb.settings_routing(on, clients)
    return texts.SETTINGS_ROOT, kb.settings_root()


async def _render(cb: CallbackQuery, sec: str, services):
    text, markup = await _screen(sec, services)
    await edit(cb, text, markup)


# ── открытие раздела ─────────────────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "open"))
async def open_section(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    await state.clear()
    await _render(cb, callback_data.sec, services)
    await cb.answer()


# ── тумблеры (bool в YAML или mute обновлений в БД) ───────────────────────────
@router.callback_query(SetCB.filter(F.act == "toggle"))
async def toggle(cb: CallbackQuery, callback_data: SetCB, services):
    key = callback_data.key
    if callback_data.sec == "upd" and key == "notify":
        # уведомления об обновлениях = мьют в БД (не YAML). never-расписание не
        # даёт включить (проверяем перед снятием мьюта).
        if str(settings.get("updates.poll_schedule", "day")).lower() == "never":
            await cb.answer("Сначала выбери расписание проверки (не «никогда»).", show_alert=True)
            return
        muted = await call(services.updates_muted)
        if muted:
            await call(services.unmute_updates)
        else:
            await call(services.mute_updates)
    else:
        cur = settings.get_bool(key, True)
        try:
            await call(settings.set_value, key, not cur)
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        # выключатель условной маршрутизации меняет состояние системы, а не
        # только значение в yaml: применяем сразу, не дожидаясь тика монитора
        if key == "app.routing.enabled":
            await call(services.reconcile_routing)
    await _render(cb, callback_data.sec, services)
    await cb.answer()


@router.callback_query(SetCB.filter((F.sec == "rt") & (F.act == "do")))
async def routing_action(cb: CallbackQuery, callback_data: SetCB, services):
    """Разрешение профилю на РФ-доступ. Верхний слой флага: снимая его, гасим
    эффект, но настройки самого клиента не разрушаем."""
    if callback_data.key != "allow":
        await cb.answer("Действие недоступно.", show_alert=True)
        return
    client = await call(services.db.get_client, int(callback_data.val or 0))
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    new_state = not client.routing_allowed
    await call(services.set_routing_allowed, client.id, new_state)
    await _render(cb, "rt", services)
    await cb.answer(f"{client.name}: РФ-доступ "
                    + ("разрешён" if new_state else "запрещён"))


# ── ввод числового значения (FSM) ────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "edit"))
async def edit_value(cb: CallbackQuery, callback_data: SetCB, state: FSMContext):
    key = callback_data.key
    if key not in texts.SETTINGS_BOUNDS:      # старая/битая клавиатура
        await cb.answer("Эта настройка недоступна.", show_alert=True)
        return
    await state.set_state(SettingsInput.value)
    await state.update_data(key=key, sec=callback_data.sec)
    await edit(cb, texts.settings_prompt(key), kb.settings_cancel(callback_data.sec))
    await cb.answer()


@router.message(SettingsInput.value)
async def receive_value(message: Message, state: FSMContext, services):
    data = await state.get_data()
    key, sec = data.get("key"), data.get("sec", "root")
    if key not in texts.SETTINGS_BOUNDS:      # рассинхрон state (не должен случаться)
        await state.clear()
        text, markup = await _screen(sec, services)
        await message.answer(text, reply_markup=markup)
        return
    lo, hi, _label, _unit = texts.SETTINGS_BOUNDS[key]
    raw = (message.text or "").strip()
    try:
        val = int(raw)
        if not (lo <= val <= hi):
            raise ValueError
    except ValueError:
        await message.answer(texts.settings_bad_value(key))
        return
    try:
        await call(settings.set_value, key, val)
    except settings.SettingsWriteError as e:
        await state.clear()
        await message.answer(str(e))
        return
    await state.clear()
    text, markup = await _screen(sec, services)
    await message.answer(text, reply_markup=markup)


# ── выбор enum (расписание обновлений) ───────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "pick"))
async def pick(cb: CallbackQuery, callback_data: SetCB, services):
    if callback_data.sec == "upd" and callback_data.key == "sched":
        opt = callback_data.val
        try:
            await call(settings.set_value, "updates.poll_schedule", opt)
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        if opt == "never":                      # никогда → авто-мьют уведомлений
            await call(services.mute_updates)
    await _render(cb, callback_data.sec, services)
    await cb.answer()


# ── действия (бэкап сейчас, рестарты, проверка обновлений) ────────────────────
@router.callback_query(SetCB.filter(F.act == "do"))
async def do_action(cb: CallbackQuery, callback_data: SetCB, services):
    key = callback_data.key
    if key == "now":                                   # бэкап сейчас
        await cb.answer("Готовлю бэкап…")
        paths = await call(services.make_backup)
        for p in paths:
            try:
                await cb.message.answer_document(FSInputFile(p))
            except Exception:                          # noqa: BLE001
                pass
        await _render(cb, "backup", services)
        return
    if key == "awg":                                   # рестарт AWG
        await cb.answer("Перезапускаю AWG…")
        try:
            await call(services.restart_service)
            # клавиатура — полным рендером раздела: голый settings_svc() терял
            # бы кнопки переезда до следующего захода в раздел
            await edit(cb, "✅ AmneziaWG перезапущен, блокировки восстановлены.",
                       (await _screen("svc", services))[1])
        except Exception as e:                         # noqa: BLE001
            await edit(cb, f"⚠️ Ошибка перезапуска AWG: {e}",
                       (await _screen("svc", services))[1])
        return
    if key == "bot":                                   # рестарт бота
        await cb.answer("Перезапускаю бота…")
        await edit(cb, "🔄 Бот перезапускается — вернётся через несколько секунд.", None)
        # Запоминаем ДО рестарта: обещание вернуться исполняет новый процесс,
        # подменяя это же сообщение панелью.
        await call(services.set_restart_wait, cb.message.chat.id, cb.message.message_id)
        await call(services.restart_bot)
        return
    if key == "check":                                 # проверить обновление сейчас
        await cb.answer("Проверяю…")
        nxt = await call(services.update_next)
        if nxt is None:
            await edit(cb, texts.update_current_ok(config.INSTALLED_VERSION),
                       kb.settings_updates(await call(services.updates_muted)))
        else:
            await edit(cb, texts.update_admin_available(config.INSTALLED_VERSION, nxt.tag, nxt.body),
                       kb.update_admin_available())
        return


# ── переезд профилей (docs/ROADMAP.md, п.3) ──────────────────────────────────
@router.callback_query(SetCB.filter((F.sec == "mig") & (F.act == "do")))
async def migration_action(cb: CallbackQuery, callback_data: SetCB, services):
    """Рычаг переезда и оба выхода.

    Ключ с восклицательным знаком — подтверждённое действие. Разделение
    намеренное: и завершение, и отмена необратимы по-разному, и оба обязаны
    показать последствия ДО нажатия, а не после.
    """
    key = callback_data.key
    if not await call(services.migration_available):
        await cb.answer("Переезд не настроен: пустые ключи в app.yaml.", show_alert=True)
        return
    # Сторож состояния. Колбэк приходит и из СТАРОГО сообщения в истории чата
    # (тот же класс, что у раздела маршрутизации): «finish!» с прошлогоднего
    # подтверждения, нажатый после отмены, снёс бы старые пиры орфанов и
    # заархивировал ровно то, что отмена сохранила.
    running = await call(services.migration_running)
    if key in ("pending", "finish", "cancel", "finish!", "cancel!") and not running:
        await cb.answer("Переезд сейчас не идёт — экран устарел.", show_alert=True)
        return

    if key == "start":
        await cb.answer("Рождаю двойников…")
        res = await call(services.migration_start)
        await edit(cb, texts.migration_started(res), kb.settings_back())
        return

    if key == "pending":
        rows = await call(services.migration_pending)
        await edit(cb, texts.migration_pending_text(rows), kb.settings_back())
        await cb.answer()
        return

    if key == "orphans":
        names: dict[int, str] = {}
        rows = []
        for d in await call(services.migration_orphan_twins):
            if d.client_id not in names:
                c = await call(services.db.get_client, d.client_id)
                names[d.client_id] = c.name if c else "?"
            rows.append((names[d.client_id], d.name))
        await edit(cb, texts.migration_orphans_text(rows), kb.settings_back())
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
        await edit(cb, texts.migration_finished(removed, dropped, failed),
                   kb.settings_back())
        return

    if key == "cancel!":
        await cb.answer("Отменяю…")
        moved = await call(services.migration_cancel)
        await edit(cb, texts.migration_cancelled(moved), kb.settings_back())
        return

    await cb.answer("Действие недоступно.", show_alert=True)

