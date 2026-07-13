"""E2E: слой доставки уведомлений (bot.notifier.send_notifications / notify_one).

Проверяем контракт доставки: пропуск пустого адресата, тихие часы vs force_sound,
проброс/дефолт клавиатуры, устойчивость к ошибке отправки одному из адресатов.
"""
import pytest

from awgbot.bot import notifier
from awgbot.bot import keyboards as kb
from awgbot.core import config
from awgbot.domain.services import Notification
from awgbot.util import timeutil

pytestmark = pytest.mark.e2e


class RecordingBot:
    """Ловит полный набор аргументов send_message (в т.ч. disable_notification)."""
    def __init__(self, fail_for=()):
        self.calls = []
        self._fail_for = set(fail_for)

    async def send_message(self, chat_id, text, reply_markup=None,
                           disable_notification=False, **kw):
        if chat_id in self._fail_for:
            raise RuntimeError("bot blocked by user")
        self.calls.append({"chat_id": chat_id, "text": text,
                           "reply_markup": reply_markup, "silent": disable_notification})


async def test_send_notifications_delivers_each():
    bot = RecordingBot()
    await notifier.send_notifications(bot, [
        Notification(111, "a"), Notification(222, "b")])
    assert [c["chat_id"] for c in bot.calls] == [111, 222]


async def test_send_notifications_skips_empty_recipient():
    bot = RecordingBot()
    await notifier.send_notifications(bot, [Notification(0, "no addr"),
                                            Notification(None, "also none"),
                                            Notification(333, "ok")])
    assert [c["chat_id"] for c in bot.calls] == [333]


async def test_send_notifications_survives_one_failure():
    bot = RecordingBot(fail_for={111})
    await notifier.send_notifications(bot, [
        Notification(111, "boom"), Notification(222, "still delivered")])
    # первый упал (заглушён), второй всё равно доставлен
    assert [c["chat_id"] for c in bot.calls] == [222]


async def test_quiet_hours_silences_normal_but_not_force_sound(monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS_ENABLED", True)
    monkeypatch.setattr(timeutil, "in_quiet_hours", lambda *a, **k: True)
    bot = RecordingBot()
    await notifier.send_notifications(bot, [
        Notification(111, "normal"),
        Notification(222, "loud", force_sound=True)])
    by_id = {c["chat_id"]: c for c in bot.calls}
    assert by_id[111]["silent"] is True                    # обычное — без звука
    assert by_id[222]["silent"] is False                   # force_sound пробивает тишину


async def test_quiet_hours_disabled_never_silent(monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS_ENABLED", False)
    bot = RecordingBot()
    await notifier.send_notifications(bot, [Notification(111, "x")])
    assert bot.calls[0]["silent"] is False


async def test_default_markup_is_hide_only_else_passthrough():
    bot = RecordingBot()
    custom = kb.grace_offer(42, 14)
    await notifier.send_notifications(bot, [
        Notification(111, "default"),
        Notification(222, "custom", reply_markup=custom)])
    by_id = {c["chat_id"]: c for c in bot.calls}
    assert by_id[111]["reply_markup"] is not None          # дефолт — «Скрыть»
    assert by_id[222]["reply_markup"] is custom            # своя клавиатура сохранена


async def test_notify_one_skips_empty_and_delivers(monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS_ENABLED", False)
    bot = RecordingBot()
    await notifier.notify_one(bot, 0, "skip")               # нет адресата
    await notifier.notify_one(bot, 555, "hi")
    assert [c["chat_id"] for c in bot.calls] == [555]
