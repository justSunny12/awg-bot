"""cycles.py — кнопки-циклы и выбор из ряда."""

from __future__ import annotations

import logging
from aiogram import F
from aiogram.types import CallbackQuery
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.bot import keyboards as kb
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers import settingscore as core
from awgbot.bot.handlers import updates_flow
from awgbot.bot.handlers.common import call

log = logging.getLogger("awgbot.handlers.settings")
from ._router import router
from .render import HOOKS, _render

# ── выбор enum (расписание обновлений) ───────────────────────────────────────
_RT_MON_PICKS = {
    "probe": ("app.routing.probe_seconds", ("30", "45", "60")),
    "window": ("app.routing.failover.window_samples", ("5", "10", "20")),
    "avail": ("app.routing.failover.min_availability", ("25", "50", "75")),
}


# ── кнопки-циклы: значение переставляется на следующее из ряда ──────────────
_CYCLES = {
    "email.poll_interval_sec": kb.EMAIL_POLL_CYCLE,
    "email.resume_code_len": kb.EMAIL_CODE_CYCLE,
    "updates.poll_schedule": kb.UPDATE_SCHEDULE_CYCLE,
    "app.routing.probe_seconds": (30, 45, 60),
    "app.routing.failover.window_samples": (5, 10, 20),
    "app.routing.failover.min_availability": (25, 50, 75),
    "app.routing.lists_refresh_hours": (6, 12, 24),
}


def _next_in_cycle(key: str, current) -> object:
    """Следующее значение ряда; значение вне ряда (из конфига руками) —
    ближайшее большее, за последним — первое."""
    values = list(_CYCLES[key])
    try:
        cur = type(values[0])(current)
    except (ValueError, TypeError):
        return values[0]
    if cur in values:
        return values[(values.index(cur) + 1) % len(values)]
    if isinstance(cur, (int, float)):
        bigger = [v for v in values if v > cur]
        return bigger[0] if bigger else values[0]
    return values[0]


@router.callback_query(SetCB.filter(F.act == "cycle"))
async def cycle(cb: CallbackQuery, callback_data: SetCB, services):
    """Цикл вместо ввода: опрос почты, длина кода, расписание проверки
    обновлений, канал бэкапа, параметры РФ-доступа. Итог — всплывашкой."""
    key = callback_data.key
    if key == "app.scheduler.backup_channel":
        cur = str(settings.get(key, "telegram") or "telegram").lower()
        await core.set_backup_channel(cb, services, HOOKS, "email" if cur == "telegram" else "telegram")
        return
    if key not in _CYCLES:
        await cb.answer("Кнопка устарела — открой раздел заново", show_alert=True)
        return
    new = _next_in_cycle(key, settings.get(key, _CYCLES[key][0]))
    try:
        await call(settings.set_value, key, new)
    except settings.SettingsWriteError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer(texts.cycle_toast(key, new))
    await _render(cb, callback_data.sec, services, "cached" if callback_data.sec == "upd" else "")


@router.callback_query(SetCB.filter(F.act == "pick"))
async def pick(cb: CallbackQuery, callback_data: SetCB, services):
    if callback_data.sec == "rt" and callback_data.key == "lists":
        hours = callback_data.val
        if hours not in ("3", "6", "12", "24"):
            await cb.answer("Нет такого варианта", show_alert=True)
            return
        try:
            await call(settings.set_value, "app.routing.lists_refresh_hours", int(hours))
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await _render(cb, "rt_lists", services)
        await cb.answer()
        return
    if callback_data.sec == "rt" and callback_data.key in _RT_MON_PICKS:
        # мониторинг и резервирование: такт зонда, окно, порог — горячие ключи;
        # такт переставляет задачу планировщика сам (см. scheduler.HOT)
        setting, allowed = _RT_MON_PICKS[callback_data.key]
        if callback_data.val not in allowed:
            await cb.answer("Нет такого варианта", show_alert=True)
            return
        try:
            await call(settings.set_value, setting, int(callback_data.val))
        except settings.SettingsWriteError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await _render(cb, "rt_mon", services)
        await cb.answer()
        return
    if callback_data.sec == "backup" and callback_data.key == "channel":
        await core.set_backup_channel(cb, services, HOOKS, callback_data.val)
        return
    if callback_data.sec == "upd" and callback_data.key == "sched":
        if not await updates_flow.set_schedule(cb, services, callback_data.val):
            return
    await _render(cb, callback_data.sec, services)
    await cb.answer()
