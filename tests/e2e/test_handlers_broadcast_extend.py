"""E2E: объявление — один экран адресатов с тумблером «С продлением
подписки» и «Выбрать все», дни продления пресетами [1] [3] [7], превью с
шапкой, отправка: сначала продление, потом рассылка, шапка у каждого своя,
бессрочному — без шапки.

Цена ошибки: не активировавший профиль останется отмеченным при продлении —
объявление некуда доставить, а отчёт соврёт; бессрочному уйдёт строка
«продлена» — неправда; «✅ Выбрать все» не снимет отметки — разошлёшь не
тем.
"""
import datetime

import pytest

from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen
from awgbot.bot.callbacks import BroadcastCB, PresetCB
from awgbot.bot.handlers.admin import broadcast as admin_h
from awgbot.util import timeutil
import awgbot.core.config as cfg

pytestmark = pytest.mark.e2e


def _cb(bot):
    nav = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=bot)
    return FakeCallback(message=nav, user_id=cfg.ADMIN_ID, bot=bot), nav


async def _press(handler, bot, *args):
    cb, nav = _cb(bot)
    await handler(cb, *args)
    return last_screen(nav)


async def test_entry_is_the_targets_screen_with_the_extend_toggle(services, make_active_client, fake_bot):
    """Экрана выбора режима больше нет: сразу адресаты; режим — тумблером.
    С продлением не активировавшие исчезают из списка и снимаются с отметки."""
    make_active_client("Анна", tg_id=9101)
    pending = services.create_client("Ждёт", 1, "year", 0)            # не активировал
    state = FakeState()
    text, labels = await _press(admin_h.broadcast_pick, fake_bot, state, services)
    assert text == ("📢 Объявление · отмечено 0\n"
                    "Получат владельцы и те, с кем они делятся устройствами"), text
    assert labels == ["☑️ С продлением подписки", "☑️ Выбрать все", "☑️ Анна", "☑️ Ждёт",
                      "⬅️ Отмена", "➡️ Далее"], labels
    assert await state.get_state() == "Broadcast:targets"

    await _press(admin_h.broadcast_toggle, fake_bot, BroadcastCB(action="tgl", ref=pending.client_id),
                 state, services)
    text, labels = await _press(admin_h.broadcast_extend_toggle, fake_bot, state, services)
    assert text.startswith("📢 Объявление с продлением · отмечено 0\n"
                           "Получат только владельцы профилей с подпиской\n"), text
    assert labels[0] == "✅ С продлением подписки"
    assert not any("Ждёт" in l for l in labels), "не активировавший доступ попал в адресаты продления"
    assert (await state.get_data())["targets"] == [], "отметка не активировавшего пережила тумблер"

    text, labels = await _press(admin_h.broadcast_extend_toggle, fake_bot, state, services)
    assert labels[0] == "☑️ С продлением подписки" and "☑️ Ждёт" in labels
    assert not any("🟢" in l or "🔴" in l for l in labels), "онлайн-кружки вернулись"


async def test_mark_all_by_hand_turns_bulk_on_and_bulk_on_clears_everything(
        services, make_active_client, fake_bot):
    """Правило массового выбора: все отмечены по одному — «✅ Выбрать все»;
    нажатие на ✅ снимает все отметки; на ☑️ — отмечает всех."""
    a = make_active_client("Анна", tg_id=9102)
    b = make_active_client("Борис", tg_id=9103)
    state = FakeState()
    await _press(admin_h.broadcast_pick, fake_bot, state, services)
    await _press(admin_h.broadcast_toggle, fake_bot, BroadcastCB(action="tgl", ref=a.id), state, services)
    text, labels = await _press(admin_h.broadcast_toggle, fake_bot, BroadcastCB(action="tgl", ref=b.id),
                                state, services)
    assert text.startswith("📢 Объявление · отмечено 2"), text
    assert labels[1:4] == ["✅ Выбрать все", "✅ Анна", "✅ Борис"], labels

    text, labels = await _press(admin_h.broadcast_toggle_all, fake_bot, state, services)
    assert (await state.get_data())["targets"] == [], "✅ не сняла отметки"
    assert labels[1:4] == ["☑️ Выбрать все", "☑️ Анна", "☑️ Борис"], labels

    text, labels = await _press(admin_h.broadcast_toggle_all, fake_bot, state, services)
    assert sorted((await state.get_data())["targets"]) == sorted([a.id, b.id])
    assert labels[1] == "✅ Выбрать все"


async def test_next_without_targets_refuses(services, make_active_client, fake_bot):
    from awgbot.bot import texts
    make_active_client("Анна", tg_id=9104)
    state = FakeState()
    await state.set_data({"targets": [], "extend": False})
    cb, _ = _cb(fake_bot)
    await admin_h.broadcast_next(cb, state, services)
    assert cb.answers[-1] == (texts.BROADCAST_NO_TARGETS, True)


async def test_next_asks_days_by_presets_and_refuses_only_unlimited(services, make_active_client, fake_bot):
    from awgbot.bot import texts
    services.bot_username = "awg_test_bot"
    a = make_active_client("Анна", tg_id=9110)
    u = make_active_client("Борис", tg_id=9111, period_kind="never")
    state = FakeState()
    await state.set_data({"targets": [u.id], "extend": True})
    cb, nav = _cb(fake_bot)
    await admin_h.broadcast_next(cb, state, services)
    assert cb.answers[-1] == (texts.BROADCAST_ALL_UNLIMITED, True), "отмечены одни бессрочные — продлевать некого"

    await state.set_data({"targets": [a.id, u.id], "extend": True})
    text, labels = await _press(admin_h.broadcast_next, fake_bot, state, services)

    def link(c):
        return f'<a href="https://t.me/awg_test_bot?start=cl-{c.id}">{c.name}</a>'
    assert text == (f"📢 Профили для продления подписки: {link(a)}, {link(u)} (∞, без продления)\n"
                    "На сколько дней продлеваем?"), text
    assert labels == ["1 дн.", "3 дн.", "7 дн.", "✏️ Другое", "✖️ Отмена", "⬅️ Назад"], labels
    assert await state.get_state() == "Broadcast:days"
    # раскладка: пресеты рядом, «Другое» с отменой, «Назад» к адресатам своим рядом —
    # иначе «Назад» из дней выбросит из объявления вместе с отметками
    from awgbot.bot import keyboards as kbs
    markup = kbs.broadcast_days_kb()
    rows = [[b.text for b in r] for r in markup.inline_keyboard]
    assert rows == [["1 дн.", "3 дн.", "7 дн."], ["✏️ Другое", "✖️ Отмена"], ["⬅️ Назад"]], rows
    assert markup.inline_keyboard[2][0].callback_data == BroadcastCB(action="targets").pack(), \
        "«⬅️ Назад» из дней обязан вести к адресатам, а не отменять объявление"
    assert markup.inline_keyboard[1][1].callback_data == BroadcastCB(action="cancel").pack()


async def test_days_preset_moves_to_the_text_prompt(services, make_active_client, fake_bot):
    a = make_active_client("Анна", tg_id=9115)
    state = FakeState()
    await state.set_data({"targets": [a.id], "extend": True})
    await state.set_state("Broadcast:days")
    text, labels = await _press(admin_h.broadcast_days_preset, fake_bot,
                                PresetCB(kind="bc_days", val=7), state, services)
    assert (await state.get_data())["days"] == 7 and await state.get_state() == "Broadcast:text"
    assert text.startswith("📢 Текст для профиля Анна · продление на 7 дней\n"), text
    assert labels == ["⬅️ Отмена"]


async def test_days_other_validates_then_prompts_for_text(services, make_active_client, fake_bot):
    from awgbot.bot import texts
    a = make_active_client("Анна", tg_id=9120)
    state = FakeState()
    await state.set_data({"targets": [a.id], "extend": True})
    await state.set_state("Broadcast:days")
    text, labels = await _press(admin_h.broadcast_days_preset, fake_bot,
                                PresetCB(kind="bc_days", val=-1), state, services)
    assert text == "✏️ Дней, 1–365" and labels == ["✖️ Отмена"], (text, labels)

    bad = FakeMessage(text="400", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_days(bad, state, services)
    assert [s[1] for s in bad.sent if s[0] == "answer"] == [texts.BROADCAST_DAYS_BAD]
    assert "days" not in await state.get_data()

    ok = FakeMessage(text="10", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_days(ok, state, services)
    assert (await state.get_data())["days"] == 10 and await state.get_state() == "Broadcast:text"
    prompt = [s for s in ok.sent if s[0] == "answer"][-1]
    assert prompt[1].startswith("📢 Текст для профиля Анна · продление на 10 дней\n"), prompt[1]
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
    d0, d1 = timeutil.fmt_date_ui(old), timeutil.fmt_date_ui(old + datetime.timedelta(days=10))
    assert preview.startswith("👆 Так увидят получатели · 1 адресат · продление на <b>10 дней</b>:\n"
                              f"• Анна: {d0} → {d1}\n\n"), preview
    assert preview.endswith(f"<b>Подписка продлена на 10 дней 🙂\n{d0} → {d1}</b>\n\nСпасибо за терпение"), preview
    # подписка ещё не тронута — превью ничего не применяет
    assert services.db.get_client(a.id).period_end == a.period_end


async def test_send_extends_first_then_delivers_personal_headers(services, make_active_client,
                                                                 fake_bot):
    """Продление — до рассылки и всем отмеченным, у каждого шапка со своими
    датами; бессрочному объявление уходит, но без строки о продлении;
    держатель переданного устройства объявление не получает; отчёт — с
    продлением и оговоркой про бессрочного."""
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
    assert sent[9140] == (f"<b>Подписка продлена на 10 дней 🙂\n{timeutil.fmt_date_ui(old)} → "
                          f"{timeutil.fmt_date_ui(old + datetime.timedelta(days=10))}</b>\n\n"
                          "Спасибо за терпение"), sent[9140]
    assert sent[9141] == "Спасибо за терпение", "бессрочному ушла строка о продлении"
    new_e = services.db.get_client(e.id)
    assert sent[9142].startswith("<b>К твоей подписке добавлено 10 дней с текущей даты 🙂\n"
                                 f"Теперь срок подписки: до {timeutil.fmt_date_ui(timeutil.parse_iso(new_e.period_end))}</b>")
    assert new_e.status == "active"
    assert timeutil.parse_iso(services.db.get_client(a.id).period_end) == old + datetime.timedelta(days=10)
    assert services.db.get_client(u.id).period_end is None
    report = [s[1] for s in nav.sent if s[0] == "answer" and s[1].startswith("✅ Доставлено")][-1]
    assert report == ("✅ Доставлено: профили Анна, Борис, Вера — 3 адресата"
                      " · продлено на 10 дней (Борис — без продления)"), report
    assert (await state.get_data()) == {}
