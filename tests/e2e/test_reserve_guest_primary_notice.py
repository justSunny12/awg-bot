"""Бот основы на резерве сообщает админу, что основа снова в строю (Т-РУ6,
runtime/main/guest.py: _primary_back_notice) — один раз на вид сообщения.

Цена ошибки: сообщение «сервер в строю» на каждом такте (раз в несколько
секунд) — чат админа забит; «возвращаю клиентов» при рычаге «по кнопке» —
админ ждёт возврата, которого не будет; «верни кнопкой» без кнопки — админ
ищет то, чего нет.
"""
from __future__ import annotations

import pytest

from awgbot.bot import notifier
from awgbot.core import config
from awgbot.runtime.main import guest as guest_main

pytestmark = pytest.mark.e2e


@pytest.fixture()
def said(monkeypatch):
    out: list[tuple] = []

    async def notify_one(bot, chat_id, text, **kw):
        out.append((chat_id, text))
    monkeypatch.setattr(notifier, "notify_one", notify_one)
    return out


async def _tick(services, said_list, told, **state):
    services.guest_state_in(state)
    await guest_main._primary_back_notice(object(), services, told)


async def test_the_immediate_lever_says_the_clients_are_being_returned_once(services, said):
    told: dict = {}
    for _ in range(3):
        await _tick(services, said, told, primary={"status": "alive", "healthy": True}, **{"return": "immediate"})
    assert said == [(config.ADMIN_ID, "🟢 Сервер AWG снова в строю — возвращаю пользователей и бот")]


async def test_the_button_lever_says_to_press_the_button_once(services, said):
    told: dict = {}
    for _ in range(3):
        await _tick(services, said, told, primary={"status": "alive", "healthy": True, "can_return": True},
                    **{"return": "button"})
    assert said == [(config.ADMIN_ID, "🟢 Сервер AWG снова в строю — верни пользователей кнопкой")]


@pytest.mark.parametrize("state", [
    {"primary": {"status": "dead"}, "return": "immediate"},
    {"primary": {"status": "alive", "healthy": False}, "return": "immediate"},
    {"primary": {"status": "alive", "healthy": True, "can_return": False}, "return": "button"},
    {"primary": {"status": "alive", "healthy": True}, "return": "button"},
    {},
])
async def test_nothing_is_said_while_the_primary_is_not_ready_for_the_return(services, said, state):
    await _tick(services, said, {}, **state)
    assert said == [], "сообщение о возврате, пока возвращать рано или кнопки нет"
