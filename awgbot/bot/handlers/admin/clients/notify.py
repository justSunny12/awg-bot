"""notify.py — часть пакета clients (см. __init__): тумблер «уведомить пользователя»
на экранах правки условий профиля администратором.

Один тумблер на все правки условий (лимит устройств, лимит трафика, период,
продление, снятие паузы): «✅ Уведомить» / «☑️ Не уведомлять», по умолчанию
включён. Состояние живёт в FSM-данных (notify) — колбэки пресетов и
приглашения к вводу своего места под него не имеют; нажатие тумблера
перерисовывает тот же экран. Блокировка держит тумблер в колбэке — у неё
экран без FSM (handlers/admin/blocks.py)."""
from __future__ import annotations

from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.bot.callbacks import ClientCB
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.states import EditPeriod
from awgbot.domain.services.types import Notification
from awgbot.bot.handlers.admin.panel import _bot
from .card import router


async def notify_on(state: FSMContext) -> bool:
    """Состояние тумблера; по умолчанию уведомляем."""
    return bool((await state.get_data()).get("notify", True))


async def reset_notify(state: FSMContext) -> None:
    """Новый экран правки — тумблер в умолчание."""
    await state.update_data(notify=True)


def client_notes(client, notify: bool, text: str = "", notes=()) -> list:
    """Уведомления пользователю о правке: при выключенном тумблере — ничего ему (чужие
    в списке остаются: держатели его устройств); при включённом — notes и,
    если среди них нет адресованного пользователю, text с кнопкой «💳 Подписка»."""
    tg = int(getattr(client, "tg_id", 0) or 0)
    from awgbot.core import config
    if not tg or tg == config.ADMIN_ID:
        return [n for n in notes if n.tg_id != tg]
    if not notify:
        return [n for n in notes if n.tg_id != tg]
    out = list(notes)
    if text and not any(n.tg_id == tg for n in out):
        out.append(Notification(tg, text, action=("sub", 0)))
    return out


@router.callback_query(ClientCB.filter(F.action.startswith("ntf_")))
async def notify_toggle(cb: CallbackQuery, callback_data: ClientCB, services, state: FSMContext):
    """Переключить тумблер и перерисовать экран, на котором он стоит."""
    screen = callback_data.action[len("ntf_"):]
    client = await call(services.db.get_client, callback_data.client_id)
    if client is None:
        await cb.answer(ui.Toast.no_profile, show_alert=True)
        return
    notify = not await notify_on(state)
    await state.update_data(notify=notify)
    if screen == "cli_devs":
        used = await call(services.db.count_devices, client.id)
        await edit(cb, texts.devs_limit_prompt(client, used), kb.devs_limit_kb(client.id, notify))
    elif screen == "cli_traffic":
        await edit(cb, texts.traffic_limit_prompt(client), kb.traffic_limit_kb(client.id, notify))
    elif screen == "period":
        end = await state.get_state() == EditPeriod.end.state
        prompt = texts.period_end_prompt(client) if end else texts.period_start_prompt(client)
        await edit(cb, prompt, kb.period_input_kb(client.id, end=end, notify=notify))
    elif screen == "extend":
        from .extend import extend_screen
        data = await state.get_data()
        from awgbot.bot.callbacks import Menu
        screen_parts = await extend_screen(
            services, client.id, keep=bool(data.get("keep", True)), notify=notify,
            cancel_to=Menu(action="expiring").pack() if data.get("return_to") == "expiring" else None)
        if screen_parts is not None:
            await edit(cb, *screen_parts)
    elif screen == "resume":
        await edit(cb, texts.resume_ask(client, _bot(services)), kb.resume_pause_kb(client.id, notify))
    await cb.answer()
