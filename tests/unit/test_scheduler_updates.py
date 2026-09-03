"""Уведомление «доступна следующая версия»: буква v и снятие «В меню» у
прошлого финишера."""
from __future__ import annotations

import types

import pytest

from awgbot.bot import texts
from awgbot.runtime import scheduler as sched


def test_versions_carry_v_prefix():
    assert "Текущая версия бота v2.4.2.8." in texts.update_available("v2.4.2.9", "", "2.4.2.8")
    assert "Доступна новая версия: v2.4.2.9" in texts.update_available("v2.4.2.9", "", "2.4.2.8")
    assert "(v2.4.2.8) актуальна" in texts.update_current_ok("2.4.2.8")
    assert texts.update_admin_available("2.4.2.8", "v2.4.2.9", "").startswith(
        "Текущая версия бота v2.4.2.8.\nДоступно обновление до v2.4.2.9")
    # тег уже с буквой — не удваиваем
    assert "vv" not in texts.update_admin_available("v2.4.2.8", "v2.4.2.9", "")


@pytest.mark.asyncio
async def test_notify_update_available_dismisses_previous_finisher(monkeypatch):
    calls = []

    async def dismiss(bot, services, keep=None):
        calls.append("dismiss")

    async def send(bot, notes):
        calls.append(("send", notes[0].text.splitlines()[1]))

    monkeypatch.setattr("awgbot.bot.handlers.common.dismiss_update_reports", dismiss)
    monkeypatch.setattr("awgbot.bot.notifier.send_notifications", send)
    nxt = types.SimpleNamespace(tag="v2.4.2.9", body="- x")
    await sched.notify_update_available(object(), object(), nxt)
    assert calls == ["dismiss", ("send", "Доступна новая версия: v2.4.2.9")]
