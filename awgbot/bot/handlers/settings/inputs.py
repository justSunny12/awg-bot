"""inputs.py — свой DNS-резолвер, порты, ввод значений."""

from __future__ import annotations

import logging
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.bot.roles import MAIN
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import SetCB
from awgbot.bot.states import MigrationPort, SshPort
from awgbot.bot.handlers import settingscore as core
from awgbot.domain.services import ServiceError
from awgbot.bot.handlers.common import call, edit, send_menu, ask_here, ask_tracked, cleanup_content

log = logging.getLogger("awgbot.handlers.settings")
from ._router import router
from .render import _render, _screen, _shared

# ── свой DNS-резолвер: три решения ───────────────────────────────────────────
# Выше do_action по той же причине, что и остальные специфичные обработчики:
# его фильтр F.act == "do" перехватил бы sec="dns" и промолчал.
@router.callback_query(SetCB.filter((F.sec == "dns") & (F.act == "do")))
async def private_dns_action(cb: CallbackQuery, callback_data: SetCB, services, state: FSMContext):
    key = callback_data.key
    if key == "now":
        # Решение записано, дальше — обычная подготовка переезда: она читает
        # его и говорит, что DNS клиентов станет своим.
        await call(services.set_private_dns_decision, "pending")
        blocked = await call(services.migration_blocked_reason)
        if blocked:
            await cb.answer(f"Сейчас переезд невозможен: {blocked}", show_alert=True)
            await _render(cb, "srv", services)
            return
        await state.clear()
        await _render(cb, "mig_prep", services)
        await cb.answer("Записал: следующий переезд — со своим резолвером")
        return
    if key == "later":
        await call(services.set_private_dns_decision, "pending")
        await edit(cb, texts.PRIVATE_DNS_LATER, kb.settings_back("srv"))
        await cb.answer()
        return
    if key == "never":
        await call(services.set_private_dns_decision, "dismissed")
        await edit(cb, texts.PRIVATE_DNS_DISMISSED, kb.settings_back("srv"))
        await cb.answer()
        return
    await cb.answer(ui.Toast.stale, show_alert=True)


# ── ввод порта для переезда ──────────────────────────────────────────────────
# Регистрируется РАНЬШЕ общего edit_value: тот ловит любой act == "edit", а
# ключ "port" в SETTINGS_BOUNDS не значится — кнопка «Задать порт» упиралась бы
# в «Кнопка устарела — открой раздел заново».
@router.callback_query(SetCB.filter((F.sec == "mig_prep") & (F.act == "edit")))
async def migration_port_ask(cb: CallbackQuery, state: FSMContext, services):
    await state.set_state(MigrationPort.value)
    await ask_here(cb, services, state, texts.MIGRATION_ASK_PORT, "set_mig_prep")
    await cb.answer()


# ── порт SSH ─────────────────────────────────────────────────────────────────
# Тоже раньше общего edit_value: ключ "port" — не настройка из SETTINGS_BOUNDS,
# а действие с проверками (занят ли порт, принял ли sshd).
@router.callback_query(SetCB.filter((F.sec == "fw") & (F.act == "edit") & (F.key == "port")))
async def ssh_port_ask(cb: CallbackQuery, state: FSMContext, services):
    st = await call(services.firewall_screen)
    if st.get("owner"):
        # Конфигом sshd владеет другая программа — отказ сразу по кнопке.
        await state.clear()
        await edit(cb, texts.ssh_owner_refusal(st, st.get("listening"), MAIN), kb.settings_back("fw"))
        await cb.answer()
        return
    await state.set_state(SshPort.value)
    await ask_here(cb, services, state, texts.ssh_port_ask(st.get("ssh_port"), MAIN), "set_fw")
    await cb.answer()


def _port_dialog(services) -> core.PortDialog:
    return core.PortDialog(
        screen=services.firewall_screen, port_key="ssh_port", sec="fw",
        owner_refusal=lambda st, listening: (
            texts.ssh_owner_refusal(st, st.get("listening") if listening is None else listening, MAIN),
            kb.settings_back("fw")),
        finisher_kb=kb.ssh_port_finisher, changed_text=texts.ssh_port_changed)


@router.message(SshPort.value)
async def ssh_port_received(message: Message, state: FSMContext, services):
    from awgbot.bot import sections as secs
    await core.port_received(message, state, services, secs.hooks_for(MAIN), _port_dialog(services))


@router.callback_query(SetCB.filter((F.sec == "fw") & (F.act == "do")
                                    & F.key.in_({"port_retry", "port_back"})))
async def ssh_port_finisher_action(cb: CallbackQuery, callback_data: SetCB, state: FSMContext,
                                   services):
    """Кнопки финишера: финишер остаётся в чате с одной «Скрыть», дальше —
    новое приглашение или раздел новым сообщением."""
    try:
        await cb.message.edit_reply_markup(reply_markup=kb.hide_only())
    except Exception:                                     # noqa: BLE001
        pass
    if callback_data.key == "port_retry":
        await state.set_state(SshPort.value)
        st = await call(services.firewall_screen)
        await send_menu(cb.message, services, texts.ssh_port_ask(st.get("ssh_port"), MAIN), kb.cancel_input("set_fw"),
                        keep_id=cb.message.message_id)         # финишер остаётся с одной «Скрыть»
    else:
        await send_menu(cb.message, services, *await _screen("fw", services), keep_id=cb.message.message_id)
    await cb.answer()


# ── ввод значения (FSM) ──────────────────────────────────────────────────────
@router.callback_query(SetCB.filter(F.act == "edit"))
async def edit_value(cb: CallbackQuery, callback_data: SetCB, state: FSMContext, services):
    """Ввод значения ролевых разделов («Сервер», «Подписки»); общих — в sections."""
    if await _shared(cb, callback_data, services, state):
        return
    from awgbot.bot import sections as secs
    await core.start_edit(cb, services, secs.hooks_for(MAIN), state, callback_data.key, callback_data.sec)


async def _migration_prepare(cb: CallbackQuery, services, want_port: str = "") -> None:
    """Поднять второй интерфейс под переезд и перезапустить бота: имя
    интерфейса читается при старте, без рестарта рычаг не появится."""
    await cb.answer("Поднимаю интерфейс…")
    await edit(cb, "🚚 Поднимаю второй интерфейс: ключи, порт, обфускация, "
                   "автозагрузка. Это несколько секунд", None)
    try:
        res = await call(services.migration_prepare,
                         int(want_port) if str(want_port).isdigit() else None)
    except Exception as e:                                # noqa: BLE001
        await cb.message.answer(texts.migration_prepare_failed(str(e)))
        return
    # перезапуск — по кнопке: бот читает интерфейсы при старте, а момент
    # выбирает человек («⬅️ Позже» — раздел напомнит)
    await send_menu(cb.message, services, texts.migration_prepared(res), kb.restart_now_or_later())


@router.message(MigrationPort.value)
async def migration_port_received(message: Message, state: FSMContext, services):
    raw = (message.text or "").strip()
    await call(services.db.add_content_msg_id, message.chat.id, message.message_id)
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        await ask_tracked(message, services, "⚠️ Порт — число от 1 до 65535, попробуй ещё раз")
        return
    await state.clear()
    d = await call(services.migration_prepare_data, int(raw))
    await cleanup_content(message.bot, services, message.chat.id)
    await send_menu(message, services, texts.migration_prepare_intro(d),
                    kb.migration_prepare_confirm(int(raw)))


async def _routing_provision(cb: CallbackQuery, services) -> None:
    """Развернуть обвязку условной маршрутизации. Долго (до минуты) и меняет
    состояние хоста, поэтому: сразу сказать, что идём, и показать итог."""
    await cb.answer("Разворачиваю…")
    await edit(cb, "🚀 Разворачиваю обвязку: dnsmasq, NAT, маршруты, линк. "
                   "Это до минуты — не нажимай ничего", None)
    try:
        tail = await call(services.routing_provision)
    except ServiceError as e:
        await send_menu(cb.message, services, texts.routing_provision_failed(str(e)), kb.settings_back("rt"))
        return
    # Интерфейс линка читается при старте: без рестарта функция спит, экран
    # «🛰 Шлюзы» так и скажет — перезапуск по кнопке, сейчас или позже.
    await send_menu(cb.message, services, texts.routing_provisioned(tail), kb.restart_now_or_later())


async def _firewall_action(cb: CallbackQuery, callback_data: SetCB, services) -> None:
    """Действия раздела «Доступ по SSH». Из чата всё применяется сразу, без
    таймера отката: чат от SSH не зависит, и любое действие отменяется той же
    кнопкой. confirm/rollback остались для таймера, который ставит CLI
    (`awg-bot firewall setup`): его кнопки приходят в чат из терминала."""
    key, val = callback_data.key, callback_data.val
    try:
        if key == "on":
            await call(services.firewall_enable)
            await cb.answer(texts.FIREWALL_ON_ALERT, show_alert=True)
        elif key == "off":
            await call(services.firewall_disable)
            await cb.answer("Фильтр снят")
        elif key == "confirm":
            await call(services.firewall_confirm)
            await cb.answer()
            await cb.message.answer(texts.firewall_confirmed())
        elif key == "rollback":
            await call(services.firewall_confirm)
            await call(services.firewall_disable)
            await cb.answer()
            await cb.message.answer(texts.firewall_rolled_back())
        elif key == "del":
            # val — номер записи в списке (см. keyboards.settings_firewall).
            # Список мог измениться с момента отрисовки: тогда честно скажем,
            # а не удалим соседа по сдвинувшемуся номеру.
            allow = (await call(services.firewall_screen)).get("raw_allow", [])
            num, _dot, tag = (val or "").partition(".")
            idx = int(num) if num.isdigit() else -1
            if not 0 <= idx < len(allow) or tag != kb.entry_tag(allow[idx]):   # без метки — не наша кнопка
                await cb.answer(ui.Toast.list_changed, show_alert=True)
                text, markup = await _screen("fw", services)
                await edit(cb, text, markup)
                return
            entry = allow[idx]
            await call(services.firewall_allow_remove, entry)
            await cb.answer(ui.toast(f"{entry} убран"))
        else:
            await cb.answer(ui.Toast.stale, show_alert=True)
            return
    except ServiceError as e:
        await cb.answer(ui.toast(e), show_alert=True)
    except Exception as e:                                # noqa: BLE001
        log.warning("firewall %s: %s", key, e)
        await cb.answer(ui.toast(f"Не вышло: {e}"), show_alert=True)
    text, markup = await _screen("fw", services)
    await edit(cb, text, markup)
