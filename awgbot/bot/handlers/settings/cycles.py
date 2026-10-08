"""cycles.py — кнопки-циклы и выбор из ряда."""

from __future__ import annotations

import logging
from aiogram import F
from aiogram.types import CallbackQuery
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.bot.callbacks import SetCB
from awgbot.bot.handlers.common import call

log = logging.getLogger("awgbot.handlers.settings")
from ._router import router
from .render import _render, _shared

# ── выбор enum (расписание обновлений) ───────────────────────────────────────
_RT_MON_PICKS = {
    "probe": ("app.routing.probe_seconds", ("30", "45", "60")),
    "window": ("app.routing.failover.window_samples", ("5", "10", "20")),
    "avail": ("app.routing.failover.min_availability", ("25", "50", "75")),
}


# ── кнопки-циклы: значение переставляется на следующее из ряда ──────────────
_CYCLES = {
    "app.routing.probe_seconds": (30, 45, 60),
    "app.routing.failover.window_samples": (5, 10, 20),
    "app.routing.failover.min_availability": (25, 50, 75),
    "app.routing.lists_refresh_hours": (6, 12, 24),
}


def _next_in_cycle(key: str, current) -> object:
    from awgbot.bot.sections import next_in_cycle
    return next_in_cycle(_CYCLES[key], current)


@router.callback_query(SetCB.filter(F.act == "cycle"))
async def cycle(cb: CallbackQuery, callback_data: SetCB, services):
    """Цикл вместо ввода у ролевых разделов — параметры РФ-доступа; циклы общих
    разделов (почта, канал бэкапа, расписание проверки) — в sections."""
    if await _shared(cb, callback_data, services):
        return
    key = callback_data.key
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
    await _render(cb, callback_data.sec, services)


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
    await _render(cb, callback_data.sec, services)
    await cb.answer()
