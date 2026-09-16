"""E2E: объявление с продлением подписки — выбор режима, адресаты, шаг дней,
превью с шапкой, отправка: сначала продление, потом рассылка, шапка у каждого
своя, бессрочному — без шапки."""
import datetime

import pytest

from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen
from awgbot.bot.callbacks import BroadcastCB
from awgbot.bot.handlers import admin as admin_h
from awgbot.util import timeutil
import awgbot.core.config as cfg

pytestmark = pytest.mark.e2e


def _cb(bot):
    nav = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    return FakeCallback(message=nav, user_id=cfg.ADMIN_ID, bot=bot), nav


async def test_entry_shows_mode_choice_then_targets(services, make_active_client, fake_bot):
    from awgbot.bot import texts
    make_active_client("Анна", tg_id=9101)
    services.create_client("Ждёт", 1, "year", 0)                    # не активировал
    state = FakeState()
    cb, nav = _cb(fake_bot)
    await admin_h.broadcast_pick(cb, state, services)
    text, labels = last_screen(nav)
    assert text == texts.BROADCAST_MODE
    assert labels == ["✉️ Простое", "💌 С продлением подписки", "⬅️ Отмена"]

    cb, nav = _cb(fake_bot)
    await admin_h.broadcast_mode(cb, BroadcastCB(action="mode", ref=1), state, services)
    text, labels = last_screen(nav)
    assert text == texts.BROADCAST_TARGETS_EXTEND
    assert "☑️ Анна" in labels and not any("Ждёт" in l for l in labels), \
        "не активировавший доступ попал в адресаты продления"
    assert (await state.get_data()) == {"targets": [], "extend": True}

    cb, nav = _cb(fake_bot)
    await admin_h.broadcast_mode(cb, BroadcastCB(action="mode", ref=0), state, services)
    text, labels = last_screen(nav)
    assert text == texts.BROADCAST_TARGETS and any("Ждёт" in l for l in labels)
    assert not any("🟢" in l or "🔴" in l for l in labels), "онлайн-кружки вернулись"


async def test_next_asks_days_and_refuses_only_unlimited(services, make_active_client, fake_bot):
    from awgbot.bot import texts
    a = make_active_client("Анна", tg_id=9110)
    u = make_active_client("Борис", tg_id=9111, period_kind="never")
    state = FakeState()
    await state.set_data({"targets": [u.id], "extend": True})
    cb, nav = _cb(fake_bot)
    await admin_h.broadcast_next(cb, state, services)
    assert cb.answers[-1][0] == texts.BROADCAST_ALL_UNLIMITED and cb.answers[-1][1] is True

    await state.set_data({"targets": [a.id, u.id], "extend": True})
    cb, nav = _cb(fake_bot)
    await admin_h.broadcast_next(cb, state, services)
    text, labels = last_screen(nav)
    assert "• <b>Анна</b>\n• <b>Борис</b> (∞, без продления)" in text
    assert labels == ["⬅️ Отмена"]
    assert await state.get_state() == "Broadcast:days"


async def test_days_input_validates_then_prompts_for_text(services, make_active_client, fake_bot):
    from awgbot.bot import texts
    a = make_active_client("Анна", tg_id=9120)
    state = FakeState()
    await state.set_data({"targets": [a.id], "extend": True})
    await state.set_state("Broadcast:days")
    bad = FakeMessage(text="400", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_days(bad, state, services)
    assert any(s[0] == "answer" and s[1] == texts.BROADCAST_DAYS_BAD and s[2] is not None
               for s in bad.sent), "переспрос без кнопки отмены"
    assert "days" not in await state.get_data()

    ok = FakeMessage(text="10", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_days(ok, state, services)
    data = await state.get_data()
    assert data["days"] == 10 and await state.get_state() == "Broadcast:text"
    prompt = [s for s in ok.sent if s[0] == "answer"][-1]
    assert prompt[1].startswith("✅ Принято: перед отправкой уведомления подписка адресата будет продлена на <b>10 дней</b>")
    assert prompt[2] is not None, "приглашение к тексту без кнопки отмены"


async def test_preview_carries_header_and_dates(services, make_active_client, fake_bot):
    a = make_active_client("Анна", tg_id=9130)
    state = FakeState()
    await state.set_data({"targets": [a.id], "extend": True, "days": 10})
    msg = FakeMessage(text="Спасибо за терпение", html_text="Спасибо за терпение",
                      chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_receive(msg, state, services)
    preview = [s[1] for s in msg.sent if s[0] == "answer"][-1]
    old = timeutil.parse_iso(a.period_end)
    d0, d1 = timeutil.fmt_date(old), timeutil.fmt_date(old + datetime.timedelta(days=10))
    assert (f"<b>Длительность твоей подписки увеличена на 10 дней 🙂\n{d0} → {d1}</b>\n\n"
            "Спасибо за терпение") in preview
    assert f"• <b>Анна</b>: {d0} → {d1}" in preview and "Отправляем?" in preview
    # подписка ещё не тронута — превью ничего не применяет
    assert services.db.get_client(a.id).period_end == a.period_end


async def test_send_extends_first_then_delivers_personal_headers(services, make_active_client,
                                                                 fake_bot):
    """Продление — до рассылки и всем отмеченным, у каждого шапка со своими
    датами; бессрочному — текст без шапки; держатель переданного устройства
    объявление не получает; отчёт — с продлением."""
    admin_h._last_broadcast_at.clear()
    a = make_active_client("Анна", tg_id=9140)
    u = make_active_client("Борис", tg_id=9141, period_kind="never")
    e = make_active_client("Вера", tg_id=9142)
    services.db.update_client_fields(e.id, period_end="2026-09-01T00:00:00+03:00", status="expired")
    dc = services.add_device(a.id, "Тел")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=9149)
    state = FakeState()
    await state.set_data({"targets": [a.id, u.id, e.id], "extend": True, "days": 10,
                          "text": "Спасибо за терпение"})
    cb, nav = _cb(fake_bot)
    await admin_h.broadcast_send(cb, state, services)

    sent = {r[1]: r[2] for r in fake_bot.records if r[0] == "send_message"}
    assert 9149 not in sent, "держатель получил объявление с продлением"
    old = timeutil.parse_iso(a.period_end)
    assert sent[9140].startswith(
        f"<b>Длительность твоей подписки увеличена на 10 дней 🙂\n{timeutil.fmt_date(old)} → "
        f"{timeutil.fmt_date(old + datetime.timedelta(days=10))}</b>\n\nСпасибо за терпение")
    assert sent[9141] == "Спасибо за терпение", "бессрочному ушла шапка"
    new_e = services.db.get_client(e.id)
    assert sent[9142].startswith("<b>К длительности твоей подписки добавлено 10 дней с текущей даты 🙂\n"
                                 f"Теперь срок подписки: до {timeutil.fmt_date(timeutil.parse_iso(new_e.period_end))}</b>")
    assert new_e.status == "active"
    assert timeutil.parse_iso(services.db.get_client(a.id).period_end) == old + datetime.timedelta(days=10)
    report = [s[1] for s in nav.sent if s[0] == "answer" and "Объявление выше доставлено" in s[1]][-1]
    assert "подписка продлена на 10 дней (Борис — бессрочная, без продления)." in report
    assert (await state.get_data()) == {}
