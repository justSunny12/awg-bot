"""sections.py — открытие разделов, тумблеры, действия РФ-доступа."""

from __future__ import annotations

import logging
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.core import config, settings
from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.bot import keyboards as kb
from awgbot.bot import screens
from awgbot.bot import sections as secs
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers import settingscore as core
from awgbot.bot.roles import MAIN
from awgbot.bot.notifier import send_notifications
from awgbot.domain.services import ServiceError
from awgbot.bot.handlers import pick
from awgbot.bot.handlers.common import call, edit, send_menu, show_main_menu, card_from_main, drop_message

log = logging.getLogger("awgbot.handlers.settings")
from ._router import router
from .inputs import _routing_provision
from .render import _drop_bundle_msgs, _render, _screen, _shared, card_kb

# ── открытие раздела ─────────────────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "open"))
async def open_section(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    """Ролевые разделы; общие — в sections (сюда их колбэк не доходит, вызов
    напрямую — из тестов — передаётся туда же)."""
    if await _shared(cb, callback_data, services, state):
        return
    await state.clear()
    if callback_data.sec == "rt":
        card_from_main(cb.message.chat.id, False)
    await _render(cb, callback_data.sec, services, callback_data.key or "")
    await cb.answer()



USERS_SCOPE = "rt_users"          # область отметок «👥 Кому доступен» в FSM (handlers/pick)


async def _users_screen(services, marks: dict | None = None):
    """(текст, клавиатура) «👥 Кому доступен» с отметками marks (без них —
    сохранённое, ряда «Применить» нет)."""
    clients = await call(services.routing_grantable_clients)
    saved = {c.id: bool(c.routing_allowed) for c in clients}
    marks = marks if marks is not None else saved
    from awgbot.bot import paging
    return texts.routing_users_text(), kb.settings_routing_users(
        clients, marks, dirty=bool(pick.changed(marks, saved)), page=paging.page_of(config.ADMIN_ID, "rtusers"))


@router.callback_query(SetCB.filter((F.sec == "rt_users") & (F.act == "page")))
async def routing_users_page(cb: CallbackQuery, services, state: FSMContext):
    """Листание «Кому доступен»: отметки как есть."""
    clients = await call(services.routing_grantable_clients)
    marks = await pick.load(state, USERS_SCOPE, {c.id: bool(c.routing_allowed) for c in clients})
    await edit(cb, *await _users_screen(services, marks))
    await cb.answer()


# ── тумблеры (bool в YAML или mute обновлений в БД) ───────────────────────────
@router.callback_query(SetCB.filter(F.act == "toggle"))
async def toggle(cb: CallbackQuery, callback_data: SetCB, services):
    if await _shared(cb, callback_data, services):
        return
    key = callback_data.key
    if key == "app.routing.enabled" and settings.get_bool(key, False):
        # Выключение бьёт по всем, кому фича разрешена, — только через
        # подтверждение; включение — сразу.
        await edit(cb, texts.ROUTING_DISABLE_CONFIRM, kb.routing_disable_confirm())
        await cb.answer()
        return
    if key == "app.routing.enabled":
        # включение — сразу, со всплывашкой итога; состояние системы меняется
        # тут же, не дожидаясь тика монитора; шлюз лежит — уведомление с именем
        # и кнопкой «🛰 Шлюзы»: пока фича была выключена, о нём молчали
        try:
            await call(settings.set_value, key, True)
        except settings.SettingsWriteError as e:
            await cb.answer(ui.toast(e), show_alert=True)
            return
        await call(services.reconcile_routing)
        await _render(cb, "rt", services)
        await cb.answer("РФ-доступ включён")
        if await call(services.db.gateways):
            ok, _reason = await call(services.routing_status)
            if ok:
                try:
                    active = await call(services.active_gateway)
                    link = services._gw_link(active) if active is not None else ""
                except Exception:                      # noqa: BLE001 — имя шлюза не стоит молчания о нём
                    link = ""
                warn = texts.routing_gateway_warning(await call(services.routing_probe), at_start=False, gw=link)
                if warn:
                    await cb.message.answer(warn, reply_markup=kb.append_hide_row(
                        ui.rows(("🛰 Шлюзы", SetCB(sec="rt")))))
        return

    if callback_data.sec == "rt_mon" and key == "app.routing.failover.enabled":
        # тумблер из «Мониторинга» 3.1.0: теперь он на экране «Шлюзы»
        new = not settings.get_bool(key, True)
        try:
            await call(settings.set_value, key, new)
        except settings.SettingsWriteError as e:
            await cb.answer(ui.toast(e), show_alert=True)
            return
        await _render(cb, "rt", services)
        await cb.answer("Автопереключение " + ("включено" if new else "выключено"))
        return

    await core.toggle_bool(cb, services, secs.hooks_for(MAIN), key, callback_data.sec)


@router.callback_query(SetCB.filter((F.sec == "rt") & (F.act == "do")))
async def routing_action(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext | None = None):
    """Разрешение профилю на РФ-доступ — отметками на экране, применяет «✅
    Применить» одной операцией (set_routing_allowed_many) с уведомлениями
    адресатам. Верхний слой флага: снимая его, гасим эффект, но настройки
    самого клиента не разрушаем."""
    if callback_data.key == "provision":
        await _routing_provision(cb, services)
        return
    if callback_data.key == "bundle":
        # «📤 Выпустить файл» в старом сообщении: файла перевыпуска больше нет —
        # кнопка устаревшая, как в обработчике устаревших кнопок
        from awgbot.bot.handlers.stale import STALE_BUTTON
        await cb.answer(STALE_BUTTON)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:                                  # noqa: BLE001
            pass
        await show_main_menu(cb.message, services, "admin")
        return
    if callback_data.key == "lists_refresh":
        # Колбэк отвечается ОДИН раз — второй ответ Telegram молча роняет.
        # Обновление занимает секунды, спиннер на кнопке их покрывает; итог —
        # числом в ответе, а свежесть видна в перерисованном блоке «Списки».
        # ответ — сразу: скачивание идёт секунды, и к концу колбэк протухал
        # (всплывашка терялась); итог — первой строкой перерисованного раздела
        await cb.answer("Обновляю списки…")
        n = await call(services.routing_update_lists, True)
        from awgbot.bot.texts.fmt import plural_ru, num
        note = f"✅ Списки обновлены: {num(n)} " + plural_ru(int(n), 'запись', 'записи', 'записей')
        text, markup = await _screen("rt_params", services)
        await edit(cb, screens.with_note(text, note), markup)
        return
    if callback_data.key == "off!":
        # подтверждённое выключение фичи целиком (см. toggle)
        try:
            await call(settings.set_value, "app.routing.enabled", False)
        except settings.SettingsWriteError as e:
            await cb.answer(ui.toast(e), show_alert=True)
            return
        await call(services.reconcile_routing)
        await _render(cb, "rt", services)
        await cb.answer("РФ-доступ выключен")
        return
    if callback_data.key == "bundle_menu":
        # кнопка файлов, выданных до 3.1.0: файл уходит из чата, главная — новым
        await drop_message(cb, services)
        await show_main_menu(cb.message, services, "admin")
        await cb.answer()
        return
    if callback_data.key in ("bundle_cancel", "bundle_home"):
        # «В карточку» / «На главную» под файлом первого применения (и
        # шифрованным файлом перевыпуска в старых сообщениях): файл и сообщение над ним (погасшая карточка, инструкция)
        # уходят из чата — внутри ключ линка, — человек возвращается в карточку
        # слота, для которого выпускал, или на главную
        slot = int(callback_data.val or 0)
        where = await call(services.gw_bundle_msg_get, slot) if slot else {}
        # запись — о последнем файле слота; «В меню» на прежнем (Telegram не дал
        # его удалить при перевыпуске) убирает только его, запись нового цела
        if where and int(where.get("file") or 0) == cb.message.message_id:
            await _drop_bundle_msgs(cb.bot, services, slot, where=where)
        else:
            try:
                await cb.bot.delete_message(cb.message.chat.id, cb.message.message_id)
            except Exception:                              # noqa: BLE001
                pass
        try:
            st = await call(services.gateway_screen_state, slot) if slot else None
        except ServiceError:
            st = None
        if st is None and slot and callback_data.key == "bundle_cancel":
            # «🛰 Карточка шлюза» под файлом снятого шлюза: сказать, а не молча на главную
            await cb.answer(texts.GATEWAY_GONE, show_alert=True)
        else:
            await cb.answer()
        if st is None or callback_data.key == "bundle_home":
            await show_main_menu(cb.message, services, "admin")
            return
        await send_menu(cb.message, services, texts.gateway_card_text(st, st["states"]),
                        card_kb(st, cb.message.chat.id))
        return
    if callback_data.key in ("allow", "allow_all", "allow_apply"):
        await _users_action(cb, callback_data, services, state)
        return
    await cb.answer(ui.Toast.stale, show_alert=True)


async def _users_action(cb: CallbackQuery, callback_data: SetCB, services, state) -> None:
    clients = await call(services.routing_grantable_clients)
    saved = {c.id: bool(c.routing_allowed) for c in clients}
    if callback_data.key == "allow_apply":
        marks = await pick.pending(state, USERS_SCOPE)
        changes = pick.changed(marks, saved) if marks is not None else {}
        if not changes:
            await pick.drop(state)
            await edit(cb, *await _users_screen(services))
            await cb.answer(ui.Toast.stale, show_alert=True)
            return
        # ответ колбэку — сразу (операция долгая: реконсиляция + dnsmasq);
        # реконсиляция одна на всех разрешённых и одна на всех запрещённых
        names = {c.id: c.name for c in clients}
        await cb.answer(ui.toast(texts.routing_allowed_toast({names[i]: on for i, on in changes.items()})))
        notes = []
        for target in (True, False):
            ids = [i for i, on in changes.items() if on is target]
            if ids:
                notes += await call(services.set_routing_allowed_many, ids, target)
        await send_notifications(cb.bot, notes)
        await pick.drop(state)
        await edit(cb, *await _users_screen(services))
        return
    marks = await pick.load(state, USERS_SCOPE, saved)
    if callback_data.key == "allow_all":
        marks = pick.toggle_all(marks)
    else:
        cid = int(callback_data.val or 0)
        if cid not in marks:
            await cb.answer(ui.Toast.no_profile, show_alert=True)
            return
        marks[cid] = not marks[cid]
    await pick.store(state, USERS_SCOPE, marks)
    await edit(cb, *await _users_screen(services, marks))
    await cb.answer()
