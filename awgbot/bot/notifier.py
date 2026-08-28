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
from aiogram.types import InputMediaPhoto

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot import keyboards as kb

log = logging.getLogger("awgbot.notifier")

_BATCH_PACING_SECONDS = 0.05         # ~20 msg/с — с запасом под лимит Telegram


def _silent_now(force_sound: bool) -> bool:
    """Слать ли БЕЗ звука: тихие часы включены, сейчас тихое окно и уведомление
    не помечено как всегда-громкое (force_sound)."""
    if force_sound or not settings.get_bool("quiet_hours.quiet_hours_enabled", True):
        return False
    return timeutil.in_quiet_hours(settings.get_int("quiet_hours.quiet_hours_start", 20), settings.get_int("quiet_hours.quiet_hours_end", 7))


async def _send(bot, tg_id, text, markup, silent):
    """Одна отправка: RetryAfter → подождать и повторить один раз; прочие
    ошибки — залогировать и продолжить рассылку. Возвращает отправленное
    сообщение либо None."""
    try:
        return await bot.send_message(tg_id, text, reply_markup=markup,
                                      disable_notification=silent)
    except TelegramRetryAfter as e:
        log.warning("flood-контроль: жду %s с и повторяю для %s", e.retry_after, tg_id)
        await asyncio.sleep(e.retry_after)
        try:
            return await bot.send_message(tg_id, text, reply_markup=markup,
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
    Ошибку отправки глотаем, как и в пакетной рассылке. Возвращает отправленное
    сообщение (None при неудаче) — финишеру self-update нужен его id, чтобы
    снять кнопку при следующей ступени."""
    if not tg_id:
        return None
    silent = _silent_now(force_sound)
    markup = reply_markup or kb.hide_only()
    return await _send(bot, tg_id, text, markup, silent)


__all__ = ["send_notifications", "notify_one"]


async def send_announcement(bot, tg_id, text: str, photos=()):
    """Объявление ОДНИМ сообщением: картинки и текст под ними.

    Текст с картинками едет ПОДПИСЬЮ к первому вложению — так Telegram и
    склеивает альбом с текстом в один пост. Отдельным сообщением было бы две
    записи в чате вместо одной.

    Кнопка «Скрыть» есть только там, где Telegram её позволяет: у альбома
    reply_markup не бывает вовсе. Одинокую картинку можно было бы снабдить
    кнопкой, но тогда одно и то же объявление выглядело бы по-разному в
    зависимости от числа вложений — а разницы этой человек не заказывал.
    """
    if not photos:
        return await bot.send_message(tg_id, text, reply_markup=kb.hide_only())
    if len(photos) == 1:
        return await bot.send_photo(tg_id, photos[0], caption=text or None)
    media = [InputMediaPhoto(media=p, caption=(text or None) if i == 0 else None)
             for i, p in enumerate(photos)]
    return await bot.send_media_group(tg_id, media=media)


async def broadcast(bot, tg_ids, text, photos=()) -> tuple[int, int]:
    """Массовая рассылка объявления по списку tg_id. Возвращает (доставлено,
    не удалось). Ошибки отправки (заблокировали бота, удалён аккаунт) считаем в
    «не удалось» и продолжаем. Пейсинг между сообщениями — как в общей рассылке
    (флуд-контроль Telegram); RetryAfter внутри _send пережидается один раз.
    parse_mode берётся дефолтный (бот сконфигурирован с HTML). Тихие часы к
    объявлениям НЕ применяем — это осознанная явная отправка админом."""
    ok = failed = 0
    first = True
    for tg_id in tg_ids:
        if not tg_id:
            continue
        if not first:
            await asyncio.sleep(_BATCH_PACING_SECONDS)
        first = False
        try:
            await send_announcement(bot, tg_id, text, photos)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await send_announcement(bot, tg_id, text, photos)
                ok += 1
            except Exception as e2:                  # noqa: BLE001
                log.warning("broadcast: не доставлено %s (после retry): %s", tg_id, e2)
                failed += 1
        except Exception as e:                       # noqa: BLE001
            log.warning("broadcast: не доставлено %s: %s", tg_id, e)
            failed += 1
    return ok, failed
