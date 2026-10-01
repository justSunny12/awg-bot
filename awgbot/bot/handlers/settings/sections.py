"""sections.py — открытие разделов, тумблеры, действия РФ-доступа."""

from __future__ import annotations

import logging
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.bot import keyboards as kb
from awgbot.bot import screens
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers import settingscore as core
from awgbot.bot.handlers import updates_flow
from awgbot.bot.notifier import send_notifications
from awgbot.domain.services import ServiceError
from awgbot.bot.handlers.common import call, edit, send_menu, show_main_menu, card_from_main

log = logging.getLogger("awgbot.handlers.settings")
from ._router import router
from .inputs import _routing_provision
from .render import HOOKS, _drop_bundle_msgs, _render, _screen, card_kb
from .slots import _issue_bundle_here

# ── открытие раздела ─────────────────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "open"))
async def open_section(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    await state.clear()
    if callback_data.sec == "rt":
        card_from_main(cb.message.chat.id, False)
    if callback_data.sec == "upd":
        await cb.answer("Проверяю…")                     # раздел ходит к списку релизов
        await _render(cb, callback_data.sec, services, callback_data.key or "")
        return
    await _render(cb, callback_data.sec, services, callback_data.key or "")
    await cb.answer()


# ── тумблеры (bool в YAML или mute обновлений в БД) ───────────────────────────
@router.callback_query(SetCB.filter(F.act == "toggle"))
async def toggle(cb: CallbackQuery, callback_data: SetCB, services):
    key = callback_data.key
    if callback_data.sec == "upd" and key == "notify":
        await updates_flow.toggle_mute(cb, services)
        await _render(cb, "upd", services, "cached")
        return
    if key == "app.routing.enabled" and settings.get_bool(key, False):
        # Выключение бьёт по всем, кому фича разрешена, — только через
        # подтверждение; включение — сразу.
        await edit(cb, texts.ROUTING_DISABLE_CONFIRM, kb.routing_disable_confirm())
        await cb.answer()
        return

    if callback_data.sec == "rt_mon" and key == "app.routing.failover.enabled":
        # тумблер из «Мониторинга» 3.1.0: теперь он на экране «Шлюзы»
        new = not settings.get_bool(key, True)
        try:
            await call(settings.set_value, key, new)
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await _render(cb, "rt", services)
        await cb.answer("Автопереключение " + ("включено" if new else "выключено"))
        return

    async def _after_set(k):
        # выключатель условной маршрутизации меняет состояние системы, а не
        # только значение в yaml: применяем сразу, не дожидаясь тика монитора
        if k == "app.routing.enabled":
            await call(services.reconcile_routing)
            # Включили — тут же замер шлюза: пока фича была выключена, о его
            # состоянии молчали (и на старте тоже), и узнать, что он лежит,
            # админ должен сейчас, а не когда пожалуются люди.
            if settings.get_bool(k, False) and await call(services.db.gateways):
                ok, _reason = await call(services.routing_status)
                if ok:
                    warn = texts.routing_gateway_warning(await call(services.routing_probe),
                                                         at_start=False)
                    if warn:
                        await cb.message.answer(f"⚠️ {warn}", reply_markup=kb.hide_only())
    await core.toggle_bool(cb, services, HOOKS, key, callback_data.sec, after_set=_after_set)


@router.callback_query(SetCB.filter((F.sec == "rt") & (F.act == "do")))
async def routing_action(cb: CallbackQuery, callback_data: SetCB, services):
    """Разрешение профилю на РФ-доступ. Верхний слой флага: снимая его, гасим
    эффект, но настройки самого клиента не разрушаем."""
    if callback_data.key == "provision":
        await _routing_provision(cb, services)
        return
    if callback_data.key == "bundle":
        # «📤 Выпустить файл» упразднённого экрана «что произойдёт» — на случай
        # старого сообщения в чате: тот же выпуск с карточки/экрана
        await cb.answer("Собираю и шифрую…")
        await _issue_bundle_here(cb, services, int(callback_data.val or 0))
        return
    if callback_data.key == "lists_refresh":
        # Колбэк отвечается ОДИН раз — второй ответ Telegram молча роняет.
        # Обновление занимает секунды, спиннер на кнопке их покрывает; итог —
        # числом в ответе, а свежесть видна в перерисованном блоке «Списки».
        # ответ — сразу: скачивание идёт секунды, и к концу колбэк протухал
        # (всплывашка терялась); итог — первой строкой перерисованного раздела
        await cb.answer("Обновляю списки…")
        n = await call(services.routing_update_lists, True)
        from awgbot.bot.texts.fmt import plural_ru
        note = (f"✅ Списки обновлены: {int(n):,} ".replace(",", " ")
                + plural_ru(int(n), 'запись', 'записи', 'записей'))
        text, markup = await _screen("rt_params", services)
        await edit(cb, screens.with_note(text, note), markup)
        return
    if callback_data.key == "off!":
        # подтверждённое выключение фичи целиком (см. toggle)
        try:
            await call(settings.set_value, "app.routing.enabled", False)
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await call(services.reconcile_routing)
        await _render(cb, "rt", services)
        await cb.answer("РФ-доступ выключен")
        return
    if callback_data.key == "bundle_menu":
        # кнопка файлов, выданных до 3.1.0: файл уходит из чата, главная — новым
        try:
            await cb.message.delete()
        except Exception:                                  # noqa: BLE001
            pass
        await show_main_menu(cb.message, services, "admin")
        await cb.answer()
        return
    if callback_data.key in ("bundle_cancel", "bundle_home"):
        # «В карточку» / «На главную» под файлом (шифрованным или первого
        # применения): файл и сообщение над ним (погасшая карточка, инструкция)
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
        await cb.answer()
        try:
            st = await call(services.gateway_screen_state, slot) if slot else None
        except ServiceError:
            st = None
        if st is None or callback_data.key == "bundle_home":
            await show_main_menu(cb.message, services, "admin")
            return
        await send_menu(cb.message, services, texts.gateway_card_text(st, st["states"]),
                        card_kb(st, cb.message.chat.id))
        return
    if callback_data.key == "allow_all":
        # правило массового выбора: ☑️ — выдать всем, ✅ — снять со всех;
        # ответ колбэку — сразу (операция долгая: реконсиляция + dnsmasq),
        # одной реконсиляцией на всех, а не по профилю
        clients = await call(services.routing_grantable_clients)
        target = not all(c.routing_allowed for c in clients)
        await cb.answer("РФ-доступ " + ("разрешён всем" if target else "не разрешён никому"))
        notes = await call(services.set_routing_allowed_many, [c.id for c in clients], target)
        await send_notifications(cb.bot, notes)
        await _render(cb, "rt_users", services)
        return
    if callback_data.key != "allow":
        await cb.answer("Действие недоступно", show_alert=True)
        return
    client = await call(services.db.get_client, int(callback_data.val or 0))
    if client is None:
        await cb.answer("Профиль не найден", show_alert=True)
        return
    new_state = not client.routing_allowed
    notes = await call(services.set_routing_allowed, client.id, new_state)
    await send_notifications(cb.bot, notes)
    await _render(cb, "rt_users", services)
    await cb.answer(f"{client.name}: РФ-доступ "
                    + ("разрешён" if new_state else "запрещён"))
