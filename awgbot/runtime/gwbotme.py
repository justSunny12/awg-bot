"""
gwbotme.py — кто такой бот шлюза: username и имя профиля по токену слота.

Основной бот хранит токен агента каждого слота (env) — по нему один `getMe`
даёт username и имя, и карточки могут вести человека в чат бота шлюза
ссылкой, а не просьбой «найди бота сам». Ответ кэшируется в state с отпечатком
токена: сменился токен — ответ спрашивается заново, иначе к Telegram за этим
не ходят вовсе.

Вызовы: после ввода токена (сразу, чтобы карточка знала бота с первого
показа), на старте и в такте живости — только для слотов, у которых токен
есть, а ответа ещё нет (сеть на старте могла быть не готова).
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# После неудачного getMe (токен отозван, сеть) слот не спрашивается раньше,
# чем через это время: иначе такт живости стучал бы в Telegram каждые полминуты
# и писал бы в журнал строку за строкой
RETRY_SECONDS = 10 * 60
_next_try: dict[int, float] = {}


async def refresh(services, slot_id: int) -> bool:
    """Спросить Telegram о боте слота и запомнить. False — токена нет или
    Telegram не ответил (ошибка в журнал, не наружу)."""
    token = services.gw_bot_token(slot_id)
    if not token:
        return False
    _next_try.pop(int(slot_id), None)             # явный вызов — без паузы
    from aiogram import Bot
    bot = Bot(token)
    try:
        me = await bot.get_me()
    except Exception as e:                                # noqa: BLE001
        log.info("бот шлюза слота %s: getMe не прошёл (%s)", slot_id, e)
        _next_try[int(slot_id)] = time.monotonic() + RETRY_SECONDS
        return False
    finally:
        try:
            await bot.session.close()
        except Exception:                                 # noqa: BLE001
            pass
    services.set_gw_bot_identity(slot_id, me.username or "", me.first_name or me.username or "")
    return True


async def ensure_all(services) -> None:
    """Дозаполнить то, чего нет: слоты с токеном, но без ответа Telegram;
    после неудачи слот ждёт RETRY_SECONDS."""
    now = time.monotonic()
    for slot_id in services.gw_bot_identity_missing():
        if _next_try.get(int(slot_id), 0.0) > now:
            continue
        await refresh(services, slot_id)
