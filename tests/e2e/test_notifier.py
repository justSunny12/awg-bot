"""E2E: слой доставки уведомлений (bot.notifier.send_notifications / notify_one).

Проверяем контракт доставки: пропуск пустого адресата, тихие часы vs force_sound,
проброс/дефолт клавиатуры, устойчивость к ошибке отправки одному из адресатов,
429 (RetryAfter): пауза ровно retry_after и один повтор, пауза дольше предела —
критичный алерт письмом сразу, прочее отложенным повтором фоном, on_sent только при
доставке, письмо критичного алерта после сетевого отказа, окно пачки при паузе;
рассылка объявления: пауза дольше предела — адресат в конец, один повтор, отчёт по итогу.
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


async def test_a_quiet_notice_is_silent_outside_quiet_hours_too(monkeypatch):
    """Разовая сводка (silent) — без звука и днём; прочие — как раньше."""
    settings.set_value("quiet_hours.quiet_hours_enabled", False)
    bot = RecordingBot()
    await notifier.send_notifications(bot, [Notification(111, "x", silent=True), Notification(222, "y")])
    by_id = {c["chat_id"]: c for c in bot.calls}
    assert by_id[111]["silent"] is True and by_id[222]["silent"] is False


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
    # дефолт — ровно одна «Скрыть» (а не «какая-то клавиатура»)
    assert by_id[111]["reply_markup"].model_dump() == kb.hide_only().model_dump()
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
        return True                                   # письмо ушло
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


# ── 429 (RetryAfter): пауза, один повтор, подтверждение и окно пачки ─────────
# Цена ошибки: повтор без паузы или второй повтор — бот глубже уходит во
# флуд-контроль; on_sent при недоставке — монитор считает алерт показанным;
# окно шире _PARALLEL при паузе посреди пачки — рассылка сама вызывает 429.

import asyncio                                                   # noqa: E402
import logging                                                   # noqa: E402
import types                                                     # noqa: E402

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter  # noqa: E402
from aiogram.methods import SendMessage                          # noqa: E402

import awgbot.core.config as _cfg                                # noqa: E402


def _ra(sec: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"),
                              message="Too Many Requests", retry_after=sec)


def _net() -> TelegramNetworkError:
    return TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="network down")


class ScriptedBot:
    """send_message по сценарию адресата: очередь исключений, исчерпана — доставлено.
    Считает попытки и одновременно летящие отправки."""
    def __init__(self, script=None, delay=0.0):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.attempts, self.delivered = [], []
        self.inflight = self.peak = 0
        self.delay = delay

    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=False, **kw):
        self.attempts.append(chat_id)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            queue = self.script.get(chat_id) or []
            if queue:
                raise queue.pop(0)
            self.delivered.append(chat_id)
            return types.SimpleNamespace(message_id=100 + len(self.delivered), chat_id=chat_id)
        finally:
            self.inflight -= 1


@pytest.fixture
def sleeps(monkeypatch):
    """Сон внутри notifier записывается, а не идёт: пауза 429 видна числом, тест
    её не ждёт. Подменён модуль asyncio только у notifier — заглушка бота и
    цикл событий спят по-настоящему."""
    seen = []
    real = asyncio.sleep
    fake = types.ModuleType("asyncio")
    fake.__dict__.update(vars(asyncio))

    async def sleep(sec, *a, **k):
        seen.append(sec)
        await real(0)
    fake.sleep = sleep
    monkeypatch.setattr(notifier, "asyncio", fake)
    return seen


@pytest.fixture
def mails():
    sent = []

    async def fb(text):
        sent.append(text)
        return True                                   # письмо ушло
    notifier.set_email_fallback(fb)
    yield sent
    notifier.set_email_fallback(None)


async def test_retry_after_waits_the_given_time_and_retries_once(sleeps):
    bot = ScriptedBot({111: [_ra(3)]})
    done = []
    await notifier.send_notifications(bot, [Notification(111, "x", on_sent=lambda: done.append(1))])
    assert bot.attempts == [111, 111] and bot.delivered == [111]
    assert sleeps == [3], "пауза — ровно retry_after"
    assert done == [1], "доставка после повтора не подтверждена (или подтверждена дважды)"


async def test_a_second_retry_after_is_not_retried_and_not_confirmed(sleeps, caplog):
    bot = ScriptedBot({111: [_ra(3), _ra(3)]})
    done = []
    with caplog.at_level(logging.WARNING, logger="awgbot.notifier"):
        await notifier.send_notifications(bot, [Notification(111, "x", on_sent=lambda: done.append(1))])
    assert bot.attempts == [111, 111], "второй повтор после второго 429"
    assert bot.delivered == [] and done == [], "on_sent при недоставке"
    assert sleeps == [3]
    assert any("после retry" in r.getMessage() for r in caplog.records)


async def test_a_network_failure_after_retry_after_sends_the_critical_alert_by_mail(sleeps, mails):
    bot = ScriptedBot({_cfg.ADMIN_ID: [_ra(2), _net(), _ra(2), _net()]})
    done = []
    # по одному: оба адресованы админу, сценарий у адресата общий
    await notifier.send_notifications(bot, [
        Notification(_cfg.ADMIN_ID, "🚨 крит", critical=True, on_sent=lambda: done.append("crit"))])
    await notifier.send_notifications(bot, [
        Notification(_cfg.ADMIN_ID, "обычное", on_sent=lambda: done.append("plain"))])
    assert len(mails) == 1 and "🚨 крит" in mails[0], mails
    assert done == ["crit"], "почта засчитана не критичному"
    assert bot.attempts.count(_cfg.ADMIN_ID) == 4


async def test_a_non_network_failure_after_retry_after_is_not_mailed(sleeps, mails):
    """Отказ по существу (заблокировал бота) после повтора — не повод для письма."""
    bot = ScriptedBot({_cfg.ADMIN_ID: [_ra(2), RuntimeError("Forbidden: bot was blocked")]})
    done = []
    await notifier.send_notifications(bot, [
        Notification(_cfg.ADMIN_ID, "🚨 крит", critical=True, on_sent=lambda: done.append(1))])
    assert mails == [] and done == []


async def test_a_long_retry_after_on_a_critical_alert_goes_to_mail_without_waiting(sleeps, mails):
    """RetryAfter(300) на критичном алерте админу: письмо сразу, пятиминутного
    ожидания нет, повтора в Telegram тоже (алерт уже доставлен)."""
    bot = ScriptedBot({_cfg.ADMIN_ID: [_ra(300)]})
    done = []
    await notifier.send_notifications(bot, [
        Notification(_cfg.ADMIN_ID, "🚨 крит", critical=True, on_sent=lambda: done.append(1))])
    await _drain_deferred()
    assert max(sleeps, default=0) <= notifier._RETRY_AFTER_CAP, f"ждали {max(sleeps)} с"
    assert len(mails) == 1 and done == [1]
    assert bot.attempts == [_cfg.ADMIN_ID], "после письма — ещё и повтор в Telegram"


async def _drain_deferred():
    """Дождаться отложенных повторов notifier (их сон подменён фикстурой sleeps)."""
    while notifier._deferred:
        await asyncio.gather(*list(notifier._deferred), return_exceptions=True)


async def test_a_long_retry_after_on_a_plain_notice_is_retried_later_not_in_place(sleeps, mails):
    """RetryAfter(300) на рядовом уведомлении: отправка возвращается сразу (слот
    окна и вызывающий не стоят пять минут), повтор — один, фоном, после паузы
    ровно retry_after; доставка подтверждается уже повтором; письма нет."""
    bot = ScriptedBot({111: [_ra(300)]})
    done = []
    await notifier.send_notifications(bot, [Notification(111, "x", on_sent=lambda: done.append(1))])
    assert bot.attempts == [111] and done == [] and sleeps == [], "ждали на месте"
    await _drain_deferred()
    assert sleeps == [300] and bot.attempts == [111, 111] and bot.delivered == [111]
    assert done == [1] and mails == []


async def test_a_long_retry_after_with_the_mail_channel_off_is_retried_later_not_confirmed(sleeps):
    """Запасной канал повешен, но почта выключена: канал отвечает False — алерт не считается
    доставленным (раньше «не бросил исключения» сходило за доставку, и критичный алерт терялся
    с отметкой «показан»); повтор — отложенный, отметка — только доставкой."""
    async def fb(text):
        return False
    notifier.set_email_fallback(fb)
    try:
        bot = ScriptedBot({_cfg.ADMIN_ID: [_ra(120)]})
        done = []
        await notifier.send_notifications(bot, [
            Notification(_cfg.ADMIN_ID, "🚨 крит", critical=True, on_sent=lambda: done.append(1))])
        assert done == [] and sleeps == [], "выключенная почта сошла за доставку"
        await _drain_deferred()
        assert sleeps == [120] and bot.delivered == [_cfg.ADMIN_ID] and done == [1]
    finally:
        notifier.set_email_fallback(None)


async def test_the_same_text_is_not_deferred_twice_while_the_first_retry_waits(sleeps):
    """Монитор создаёт тот же алерт заново каждый такт, пока отметка не взведена: пока
    первый повтор ждёт паузу, тот же текст тому же адресату не шлётся и второго повтора
    не ставит — после паузы админу приходит одно сообщение, отметку взводит оно."""
    notifier.set_email_fallback(None)
    bot = ScriptedBot({_cfg.ADMIN_ID: [_ra(120)]})
    done = []
    await notifier.send_notifications(bot, [Notification(_cfg.ADMIN_ID, "🚨 крит", on_sent=lambda: done.append("a"))])
    await notifier.send_notifications(bot, [Notification(_cfg.ADMIN_ID, "🚨 крит", on_sent=lambda: done.append("b"))])
    assert bot.attempts == [_cfg.ADMIN_ID], "дубль ушёл в Telegram"
    assert len(notifier._deferred) == 1, "второй отложенный повтор"
    await _drain_deferred()
    assert sleeps == [120] and bot.delivered == [_cfg.ADMIN_ID] and done == ["a"]
    assert notifier._pending == {}


async def test_a_long_retry_after_on_a_critical_alert_without_mail_is_retried_later(sleeps):
    """Критичный алерт, а запасного канала нет (почта не настроена): не теряется —
    тот же отложенный повтор, что у рядового."""
    notifier.set_email_fallback(None)
    bot = ScriptedBot({_cfg.ADMIN_ID: [_ra(120)]})
    sent = await notifier.notify_one(bot, _cfg.ADMIN_ID, "🚨 крит")
    assert sent is None and sleeps == []
    await _drain_deferred()
    assert sleeps == [120] and bot.delivered == [_cfg.ADMIN_ID]


async def test_retry_after_at_the_cap_still_waits_in_place(sleeps, mails):
    bot = ScriptedBot({_cfg.ADMIN_ID: [_ra(notifier._RETRY_AFTER_CAP)]})
    await notifier.send_notifications(bot, [Notification(_cfg.ADMIN_ID, "🚨 крит", critical=True)])
    assert sleeps == [notifier._RETRY_AFTER_CAP] and bot.delivered == [_cfg.ADMIN_ID] and mails == []


async def test_notify_one_returns_the_message_sent_by_the_retry(sleeps):
    settings.set_value("quiet_hours.quiet_hours_enabled", False)
    bot = ScriptedBot({555: [_ra(1)]})
    sent = await notifier.notify_one(bot, 555, "hi")
    assert sent is not None and sent.message_id == 101 and bot.attempts == [555, 555]


async def test_retry_after_mid_batch_keeps_the_window_and_delivers_all(sleeps):
    """12 адресатов, 429 у двоих посреди пачки: одновременно летящих отправок
    не больше окна (пауза держит свой слот, а не открывает лишний), окно
    действительно параллельно, все доставлены, у двоих — по повтору."""
    ids = list(range(1, 13))
    bot = ScriptedBot({4: [_ra(1)], 8: [_ra(1)]}, delay=0.01)
    await notifier.send_notifications(bot, [Notification(i, "x") for i in ids])
    assert sorted(bot.delivered) == ids
    assert len(bot.attempts) == 14 and bot.attempts.count(4) == bot.attempts.count(8) == 2
    assert 1 < bot.peak <= notifier._PARALLEL, bot.peak
    assert sleeps.count(1) == 2, sleeps


async def test_broadcast_retries_once_after_retry_after(sleeps):
    bot = ScriptedBot({5: [_ra(1)], 6: [_ra(1), _ra(1)]})
    ok, failed = await notifier.broadcast(bot, [5, 6, 7], "t", [])
    assert (ok, failed) == (2, [6])
    assert bot.attempts.count(5) == bot.attempts.count(6) == 2 and bot.attempts.count(7) == 1


# Пауза 429 дольше предела в рассылке объявления: ждать её на месте значит
# держать слот окна (и всю рассылку за ним) минутами — адресат уходит в конец,
# там после своей паузы один повтор; отчёт админу считает его по итогу повтора.

async def test_broadcast_long_retry_after_goes_to_the_end_and_is_counted(sleeps):
    ids = list(range(1, 9))
    bot = ScriptedBot({3: [_ra(300)]})
    ok, failed = await notifier.broadcast(bot, ids, "t", [])
    assert (ok, failed) == (8, []), "доставленный повтором не засчитан"
    assert bot.attempts.count(3) == 2 and bot.attempts[-1] == 3, f"повтор не в конце: {bot.attempts}"
    assert sorted(bot.delivered[:-1]) == [i for i in ids if i != 3], "прочие ждали паузу чужого адресата"
    long = [s for s in sleeps if s > notifier._RETRY_AFTER_CAP]
    assert len(long) == 1 and 299 < long[0] <= 300, f"остаток паузы перед повтором: {sleeps}"


async def test_broadcast_long_retry_after_is_retried_once_and_reported_failed(sleeps):
    bot = ScriptedBot({2: [_ra(120), _ra(120)], 4: [_ra(90), RuntimeError("bot was blocked")]})
    ok, failed = await notifier.broadcast(bot, [1, 2, 3, 4, 5], "t", [])
    assert (ok, failed) == (3, [2, 4]), "порядок отчёта — как у адресатов, отказ повтора — в недоставленных"
    assert bot.attempts.count(2) == bot.attempts.count(4) == 2, "второй повтор после второго 429"
    assert bot.attempts[-2:] == [4, 2], "конец рассылки — по сроку паузы, раньше истёкшая первой"


async def test_broadcast_retry_after_at_the_cap_still_waits_in_place(sleeps):
    bot = ScriptedBot({2: [_ra(notifier._RETRY_AFTER_CAP)]})
    ok, failed = await notifier.broadcast(bot, [1, 2, 3], "t", [])
    assert (ok, failed) == (3, []) and sleeps.count(notifier._RETRY_AFTER_CAP) == 1
    assert all(s <= notifier._RETRY_AFTER_CAP for s in sleeps), sleeps


# on_message: кому нужно само сообщение (финишер self-update — его id), тот
# получает его и после отложенного повтора, когда notify_one уже вернул None.

async def test_notify_one_hands_the_message_of_a_deferred_retry_to_on_message(sleeps):
    settings.set_value("quiet_hours.quiet_hours_enabled", False)
    got = []

    async def on_message(m):
        got.append(m.message_id)
    bot = ScriptedBot({555: [_ra(300)]})
    sent = await notifier.notify_one(bot, 555, "hi", on_message=on_message)
    assert sent is None and got == [], "ждали паузу на месте"
    await _drain_deferred()
    assert got == [101], "сообщение отложенного повтора не отдано вызывающему"


async def test_notify_one_on_message_once_on_direct_and_short_retry(sleeps):
    settings.set_value("quiet_hours.quiet_hours_enabled", False)
    got = []

    async def on_message(m):
        got.append(m.message_id)
    bot = ScriptedBot({556: [_ra(2)]})
    s1 = await notifier.notify_one(bot, 555, "a", on_message=on_message)
    s2 = await notifier.notify_one(bot, 556, "b", on_message=on_message)
    assert got == [s1.message_id, s2.message_id], got


async def test_notify_one_on_message_not_called_when_undelivered_and_its_error_is_swallowed(sleeps):
    settings.set_value("quiet_hours.quiet_hours_enabled", False)
    calls = []

    async def broken(m):
        calls.append(m)
        raise RuntimeError("база недоступна")
    bot = ScriptedBot({555: [RuntimeError("bot was blocked")]})
    assert await notifier.notify_one(bot, 555, "x", on_message=broken) is None and calls == []
    sent = await notifier.notify_one(bot, 555, "y", on_message=broken)
    assert sent is not None and len(calls) == 1, "сбой обработчика сорвал доставку"
