"""E2E: слой доставки уведомлений (bot.notifier.send_notifications / notify_one).

Проверяем контракт доставки: пропуск пустого адресата, тихие часы vs force_sound,
проброс/дефолт клавиатуры, устойчивость к ошибке отправки одному из адресатов.
"""
import pytest

from awgbot.bot import notifier
from awgbot.bot import keyboards as kb
from awgbot.core import settings
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
    settings.set_value("quiet_hours.quiet_hours_enabled", True)
    monkeypatch.setattr(timeutil, "in_quiet_hours", lambda *a, **k: True)
    bot = RecordingBot()
    await notifier.send_notifications(bot, [
        Notification(111, "normal"),
        Notification(222, "loud", force_sound=True)])
    by_id = {c["chat_id"]: c for c in bot.calls}
    assert by_id[111]["silent"] is True                    # обычное — без звука
    assert by_id[222]["silent"] is False                   # force_sound пробивает тишину


async def test_quiet_hours_disabled_never_silent(monkeypatch):
    settings.set_value("quiet_hours.quiet_hours_enabled", False)
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
    settings.set_value("quiet_hours.quiet_hours_enabled", False)
    bot = RecordingBot()
    await notifier.notify_one(bot, 0, "skip")               # нет адресата
    await notifier.notify_one(bot, 555, "hi")
    assert [c["chat_id"] for c in bot.calls] == [555]


# ── подтверждение доставки ───────────────────────────────────────────────────

async def test_state_change_waits_for_actual_delivery():
    """on_sent — отметка, которая имеет смысл, только если адресат уведомление
    получил. Монитор шлюза взводил «алерт показан» до отправки: связь у шлюза
    падает вместе с линком, алерт терялся, а «✅ ожил» приходил как первое и
    единственное слово о происшествии."""
    done = []
    bot = RecordingBot(fail_for=(222,))
    await notifier.send_notifications(bot, [
        Notification(111, "дошло", on_sent=lambda: done.append(111)),
        Notification(222, "не дошло", on_sent=lambda: done.append(222)),
    ])
    assert done == [111]


async def test_email_fallback_counts_as_delivery():
    """Критичный алерт уехал письмом — состояние менять можно: админ его
    получил, и повторять каждый тик нечего."""
    from aiogram.exceptions import TelegramNetworkError
    import awgbot.core.config as cfg

    class DeadBot:
        async def send_message(self, *a, **k):
            raise TelegramNetworkError(method=None, message="network down")

    done = []

    async def fb(text):
        pass
    notifier.set_email_fallback(fb)
    try:
        await notifier.send_notifications(DeadBot(), [
            Notification(cfg.ADMIN_ID, "🚨 линк мёртв", critical=True,
                         on_sent=lambda: done.append("mail")),
            Notification(cfg.ADMIN_ID, "обычное", critical=False,
                         on_sent=lambda: done.append("plain")),
        ])
    finally:
        notifier.set_email_fallback(None)
    assert done == ["mail"], "почта — доставка только для критичного"
