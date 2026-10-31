"""Замена сессии Bot API при обходе Telegram агента (runtime/guestlink
`_renew_session` → tgsession.renew): у новой сессии — те же посредники
запросов, что повешены при старте (отметка жизни `alive.watch`, записи
сообщений списков `msgs.attach`), те же объекты и в том же порядке.

Цена ошибки: после первого включения или выключения обхода у агента не
ведутся записи сообщений списков — «Скрыть» и правка при вытеснении молча
перестают работать до перезапуска; отметка жизни сторожа обновления по
getUpdates теряется.
"""
from __future__ import annotations

import pytest
from aiogram import Bot

from awgbot.runtime import guestlink, tgsession
from awgbot.runtime.main import alive

pytestmark = pytest.mark.unit


class _Svc:
    pass


async def test_renew_keeps_middlewares_on_each_switch(monkeypatch):
    bot = Bot("123456:DUMMY", session=tgsession.make())
    alive.watch(bot)
    from awgbot.bot.lists import msgs
    msgs.attach(bot, _Svc())
    before = list(bot.session.middleware)
    assert len(before) == 2
    monkeypatch.setattr(guestlink, "_bot", bot)
    try:
        for _ in range(2):                                   # tg on, tg off
            old = bot.session
            await guestlink._renew_session(check=False)
            assert bot.session is not old, "сессия не заменена"
            assert list(bot.session.middleware) == before, \
                f"посредники потеряны при замене сессии: {[type(m).__name__ for m in bot.session.middleware]}"
    finally:
        await bot.session.close()


def test_renew_of_session_without_middlewares():
    s = tgsession.renew(object())
    assert isinstance(s, tgsession.RetryingSession) and len(s.middleware) == 0
