"""
notifier.py — рассылка Notification-намерений, которые возвращают services.

services синхронны и не шлют сообщения сами; этот async-хелпер отправляет их
через бота. Ошибки отправки (например, клиент заблокировал бота) глотаем, чтобы
одна неудача не срывала остальную рассылку.

Flood-контроль Telegram (~30 msg/с на бота): пачки (месячный сброс, массовые
алерты) шлём с лёгким пейсингом, а 429 (RetryAfter) не глотаем как прочие
ошибки — ждём указанное время и повторяем один раз, иначе уведомление молча
терялось бы именно тогда, когда рассылка большая.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramRetryAfter

from awgbot.core import config
from awgbot.util import timeutil
from awgbot.bot import keyboards as kb

log = logging.getLogger("awgbot.notifier")

_BATCH_PACING_SECONDS = 0.05         # ~20 msg/с — с запасом под лимит Telegram


def _silent_now(force_sound: bool) -> bool:
    """Слать ли БЕЗ звука: тихие часы включены, сейчас тихое окно и уведомление
    не помечено как всегда-громкое (force_sound)."""
    if force_sound or not config.QUIET_HOURS_ENABLED:
        return False
    return timeutil.in_quiet_hours(config.QUIET_HOURS_START, config.QUIET_HOURS_END)


async def _send(bot, tg_id, text, markup, silent) -> None:
    """Одна отправка: RetryAfter → подождать и повторить один раз; прочие
    ошибки — залогировать и продолжить рассылку."""
    try:
        await bot.send_message(tg_id, text, reply_markup=markup,
                               disable_notification=silent)
    except TelegramRetryAfter as e:
        log.warning("flood-контроль: жду %s с и повторяю для %s", e.retry_after, tg_id)
        await asyncio.sleep(e.retry_after)
        try:
            await bot.send_message(tg_id, text, reply_markup=markup,
                                   disable_notification=silent)
        except Exception as e2:                      # noqa: BLE001
            log.warning("Не удалось отправить уведомление %s (после retry): %s", tg_id, e2)
    except Exception as e:                           # noqa: BLE001
        log.warning("Не удалось отправить уведомление %s: %s", tg_id, e)


async def send_notifications(bot, notifications) -> None:
    first = True
    for n in notifications or []:
        if not n.tg_id:
            continue
        if not first:
            await asyncio.sleep(_BATCH_PACING_SECONDS)
        first = False
        silent = _silent_now(getattr(n, "force_sound", False))
        markup = getattr(n, "reply_markup", None) or kb.hide_only()
        await _send(bot, n.tg_id, n.text, markup, silent)


async def notify_one(bot, tg_id, text, *, reply_markup=None, force_sound=False) -> None:
    """Разовое уведомление ТРЕТЬЕМУ ЛИЦУ (не инициатору действия) — с тихими
    часами и кнопкой «Скрыть» (по умолчанию, если reply_markup не передан).
    Для ответа самому инициатору на его же действие это НЕ нужно: там
    используем обычный message.answer (глушить эхо себе бессмысленно).
    Ошибку отправки глотаем, как и в пакетной рассылке."""
    if not tg_id:
        return
    silent = _silent_now(force_sound)
    markup = reply_markup or kb.hide_only()
    await _send(bot, tg_id, text, markup, silent)


__all__ = ["send_notifications", "notify_one"]
