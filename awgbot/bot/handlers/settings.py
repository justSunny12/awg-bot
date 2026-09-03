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
from awgbot.bot.handlers.common import call, edit, send_menu, show_main_menu
from awgbot.domain.services import ServiceError

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
        lists = await call(services.routing_lists_info) if on else None
        return (texts.settings_routing_text(on, status, lists),
                kb.settings_routing(on, clients, lists["every_hours"] if lists else 6))
    return texts.SETTINGS_ROOT, kb.settings_root()


async def _render(cb: CallbackQuery, sec: str, services):
    text, markup = await _screen(sec, services)
    await edit(cb, text, markup)


async def _record(cb: CallbackQuery, text: str, services):
    """Оставить в чате СЛЕД события и вернуть раздел следующим сообщением.

    Начало, отмена и завершение переезда — из тех событий, о которых потом
    спрашивают «когда это было и чем кончилось». Ветка настроек живёт до
    следующей навигации и унесла бы ответ с собой: экран переписывается, и от
    итога не остаётся ничего.

    Кнопок на записи нет намеренно — иначе в чате оказалось бы два живых меню,
    и инвариант «одно активное» держать было бы нечем. Раздел приходит следом
    новым сообщением, как отчёт о рассылке.
    """
    await edit(cb, text, None)
    await send_menu(cb.message, services, *await _screen("svc", services))


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
    if callback_data.key == "bundle":
        await cb.answer("Собираю и шифрую…")
        try:
            blob, name = await call(services.gw_bundle_encrypted)
        except (ServiceError, OSError) as e:
            await cb.answer(f"Не удалось: {e}", show_alert=True)
            return
        from aiogram.types import BufferedInputFile
        # Экран настроек гаснет: живым должно остаться одно меню, и это —
        # кнопка «В меню» на самом бандле. Нажатие снимет её и вернёт панель.
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                                  # noqa: BLE001
            pass
        await cb.message.answer_document(
            BufferedInputFile(blob, filename=name),
            caption="📦 Бандл шлюза, зашифрован ключом линка. Перешли его боту "
                    "шлюза — он проверит и применит сам.",
            reply_markup=kb.bundle_menu_kb())
        return
    if callback_data.key == "lists_refresh":
        # Колбэк отвечается ОДИН раз — второй ответ Telegram молча роняет.
        # Обновление занимает секунды, спиннер на кнопке их покрывает; итог —
        # числом в ответе, а свежесть видна в перерисованном блоке «Списки».
        n = await call(services.routing_update_lists, True)
        await _render(cb, "rt", services)
        await cb.answer(f"В базовом наборе {n} записей.")
        return
    if callback_data.key == "bundle_menu":
        # Файл с бандлом уходит из чата целиком — после возврата он не нужен,
        # а внутри ключ линка. Панель — новым сообщением.
        try:
            await cb.message.delete()
        except Exception:                                  # noqa: BLE001
            pass
        await show_main_menu(cb.message, services, "admin")
        await cb.answer()
        return
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
    if callback_data.sec == "rt" and callback_data.key == "lists":
        hours = callback_data.val
        if hours not in ("3", "6", "12", "24"):
            await cb.answer("Нет такого варианта.", show_alert=True)
            return
        try:
            await call(settings.set_value, "app.routing.lists_refresh_hours", int(hours))
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
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
    if key in ("start", "start!") and running:
        # зеркальная половина сторожа: «start!» со старого подтверждения,
        # нажатый уже во время переезда, пересобрал бы выдачу вслепую
        await cb.answer("Переезд уже идёт — экран устарел.", show_alert=True)
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
        await _record(cb, texts.migration_finished(removed, dropped, failed), services)
        return

    if key == "cancel!":
        await cb.answer("Отменяю…")
        moved = await call(services.migration_cancel)
        await _record(cb, texts.migration_cancelled(moved), services)
        return

    await cb.answer("Действие недоступно.", show_alert=True)

# ВЫШЕ do_action НАМЕРЕННО. Фильтры проверяются в порядке регистрации, а у
# do_action он широкий (F.act == "do") и перехватил бы sec="mig" целиком:
# ключ не подошёл бы ни к одной его ветке, функция закончилась бы молча —
# без ответа на колбэк, то есть с вечным спиннером на кнопке. По той же
# причине выше стоит и routing_action.
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
