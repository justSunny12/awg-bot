"""E2E: обработчик ошибок диспетчера (bot/errors.on_error) и единый текст отказа
(error_text / error_line / ui.toast(e)).

Стережём: обработчик, упавший исключением, не оставляет человека без ответа —
кнопка получает всплывашку, сообщение — строку «⚠️ …»; отказ проекта
(ServiceError и родня) идёт своими словами, чужое исключение (KeyError,
RuntimeError) — «внутренняя ошибка», без сырого текста исключения в чате;
отказ с HTML-разметкой во всплывашке — без тегов, в строке сообщения — как
есть, обычный — экранирован.

Цена ошибки: нажатие без ответа — спиннер на кнопке и «бот завис» у человека,
трасса только в journald; сырой текст KeyError/sqlite в чате — утечка
внутренностей и непонятный текст; «<» в имени профиля ломает разметку строки.
"""
from __future__ import annotations

import pytest

from aiogram import F
from aiogram.filters import Command

from awgbot.bot import errors, ui
from awgbot.bot.routers import make_dispatcher
from awgbot.core import access_cache, config
from awgbot.domain.services import ServiceError
from awgbot.domain.services.types import ServiceErrorHtml
from awgbot.infra.awg import AwgError
from awgbot.infra.mail import MailError
from awgbot.infra.routing import RoutingError
from tests.screens import base, harness

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


# ── текст отказа ────────────────────────────────────────────────────────────

def test_project_refusals_speak_in_their_own_words_and_others_do_not():
    assert errors.error_text(ServiceError("устройство не найдено")) == "устройство не найдено"
    for e in (AwgError("сервер AWG не ответил"), RoutingError("ipset занят"), MailError("SMTP недоступен")):
        assert errors.error_text(e) == str(e)
    assert errors.error_text(KeyError("tg_id")) == errors.INTERNAL
    assert errors.error_text(RuntimeError("database is locked")) == errors.INTERNAL
    assert "locked" not in errors.error_line(RuntimeError("database is locked"))


def test_html_refusal_is_kept_in_a_line_and_stripped_in_a_toast():
    e = ServiceErrorHtml("ключ <code>a&amp;b</code> занят")
    assert errors.error_line(e) == "⚠️ ключ <code>a&amp;b</code> занят"
    assert errors.error_text(e) == "ключ a&b занят"
    assert ui.toast(e) == "ключ a&b занят"
    plain = ServiceError("профиль <Вася> не найден")
    assert errors.error_line(plain) == "⚠️ профиль &lt;Вася&gt; не найден"
    assert ui.toast(plain) == "профиль <Вася> не найден"
    assert ui.toast_cap(plain) == "Профиль <Вася> не найден"
    assert ui.toast(KeyError("x")) == errors.INTERNAL
    assert errors.error_line(MailError("письмо не ушло"), "🔴") == "🔴 письмо не ушло"


# ── обработчик ошибок диспетчера ────────────────────────────────────────────

@pytest.fixture()
def dp(services):
    access_cache.invalidate_all()
    d = make_dispatcher("client", services, services.db, reattach=True)

    async def boom_cb(cb):
        raise RuntimeError("database is locked")

    async def refuse_cb(cb):
        raise ServiceError("шлюз занят другой операцией")

    async def boom_msg(message):
        raise KeyError("tg_id")

    # обработчики диспетчера идут раньше роутеров роли — страховка видит именно их падение
    d.callback_query.register(boom_cb, F.data == "boom")
    d.callback_query.register(refuse_cb, F.data == "refuse")
    d.message.register(boom_msg, Command("boom"))
    yield d
    access_cache.invalidate_all()


async def _run(dp, services, steps):
    session = harness.StubSession(ADMIN, base.NOW, base.PEERS)
    rec = await harness.run(dp, session, uid=ADMIN, name="A", steps=steps, services=services)
    return rec.calls


async def test_a_crashed_button_gets_an_internal_error_alert_without_raw_text(dp, services, caplog):
    calls = await _run(dp, services, [("press", "boom")])
    toasts = [c.head for c in calls if c.head.startswith("~")]
    assert toasts == [f"~! {errors.INTERNAL}"], [c.head for c in calls]
    assert not any("locked" in (c.toast or "") for c in calls)
    assert any("обработчик упал" in r.getMessage() and r.exc_info for r in caplog.records), "трассы в журнале нет"


async def test_an_uncaught_refusal_speaks_its_words(dp, services):
    calls = await _run(dp, services, [("press", "refuse")])
    assert [c.head for c in calls if c.head.startswith("~")] == ["~! Шлюз занят другой операцией"]


async def test_a_crashed_message_handler_answers_with_a_line(dp, services):
    calls = await _run(dp, services, [("text", "/boom")])
    sends = [c.body for c in calls if c.head.startswith("+ send")]
    assert sends == [f"⚠️ {errors.INTERNAL}"], [c.head for c in calls]


async def test_an_untouched_dispatcher_still_answers_normally(dp, services):
    """Страховка не мешает здоровым обработчикам: /start рисует меню."""
    calls = await _run(dp, services, [("start", "")])
    assert any(c.head.startswith("+ send") for c in calls), [c.head for c in calls]
    assert not any(c.head.startswith("~") for c in calls)
