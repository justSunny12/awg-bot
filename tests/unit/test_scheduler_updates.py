"""Уведомление «доступна следующая версия»: буква v и снятие «В меню» у
прошлого финишера."""
from __future__ import annotations

import types


from awgbot.bot import texts
from awgbot.runtime import scheduler as sched


def test_versions_carry_v_prefix():
    assert "Текущая версия бота v2.4.2.8." in texts.update_available("v2.4.2.9", "", "2.4.2.8")
    assert "Доступна новая версия: v2.4.2.9" in texts.update_available("v2.4.2.9", "", "2.4.2.8")
    # тег уже с буквой — не удваиваем
    assert "vv" not in texts.update_available("v2.4.2.9", "", "v2.4.2.8")


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


def test_updates_sections_show_current_version_with_v():
    """Голое «2.4.2.12» Telegram рисует ссылкой на IP — версия с буквой v."""
    assert texts.settings_upd_text("2.4.2.12") == "⬆️ <b>Обновления</b> · v2.4.2.12 🟢 актуальна"
    assert "vv" not in texts.settings_upd_text("v2.4.2.12")


def test_admin_panel_title_carries_hostname(monkeypatch):
    monkeypatch.setattr(texts.admin, "_HOSTNAME", "vps-1")   # имя хоста кэшируется на процесс
    out = texts.admin_panel({"ok": True})
    assert out.startswith("🛠 <b>vps-1</b> 🟢"), out
