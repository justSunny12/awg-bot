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
    """Вход — сразу экран адресатов (диалог в состоянии выбора); тумблер «С
    продлением» снимает отметку с не активировавшего: продлевать и доставлять
    ему нечего, а отчёт посчитал бы его адресатом."""
    make_active_client("Анна", tg_id=9101)
    pending = services.create_client("Ждёт", 1, "year", 0)            # не активировал
    state = FakeState()
    await _press(admin_h.broadcast_pick, fake_bot, state, services)
    assert await state.get_state() == "Broadcast:targets"

    await _press(admin_h.broadcast_toggle, fake_bot, BroadcastCB(action="tgl", ref=pending.client_id),
                 state, services)
    await _press(admin_h.broadcast_extend_toggle, fake_bot, state, services)
    assert (await state.get_data())["targets"] == [], "отметка не активировавшего пережила тумблер"


async def test_mark_all_by_hand_turns_bulk_on_and_bulk_on_clears_everything(
        services, make_active_client, fake_bot):
    """Правило массового выбора: все отмечены по одному — «✅ Выбрать все»
    (снимок adm.bc.by_hand); нажатие на ✅ снимает все отметки; на ☑️ —
    отмечает всех."""
    a = make_active_client("Анна", tg_id=9102)
    b = make_active_client("Борис", tg_id=9103)
    state = FakeState()
    await _press(admin_h.broadcast_pick, fake_bot, state, services)
    await _press(admin_h.broadcast_toggle, fake_bot, BroadcastCB(action="tgl", ref=a.id), state, services)
    await _press(admin_h.broadcast_toggle, fake_bot, BroadcastCB(action="tgl", ref=b.id), state, services)
    assert sorted((await state.get_data())["targets"]) == sorted([a.id, b.id])

    await _press(admin_h.broadcast_toggle_all, fake_bot, state, services)
    assert (await state.get_data())["targets"] == [], "✅ не сняла отметки"

    await _press(admin_h.broadcast_toggle_all, fake_bot, state, services)
    assert sorted((await state.get_data())["targets"]) == sorted([a.id, b.id])


async def test_next_with_unlimited_and_finite_asks_days(services, make_active_client, fake_bot):
    """Отмечены бессрочный и конечный — шаг дней (экран с «(∞, без
    продления)» — снимок adm.bc.next.ext.mixed; одни бессрочные — отказ,
    adm.bc.next.unlimited)."""
    a = make_active_client("Анна", tg_id=9110)
    u = make_active_client("Борис", tg_id=9111, period_kind="never")
    state = FakeState()
    await state.set_data({"targets": [a.id, u.id], "extend": True})
    await _press(admin_h.broadcast_next, fake_bot, state, services)
    assert await state.get_state() == "Broadcast:days"


async def test_days_preset_moves_to_the_text_prompt(services, make_active_client, fake_bot):
    """Пресет дней записан, диалог ждёт текст (приглашение — снимок adm.bc.days.one)."""
    a = make_active_client("Анна", tg_id=9115)
    state = FakeState()
    await state.set_data({"targets": [a.id], "extend": True})
    await state.set_state("Broadcast:days")
    await _press(admin_h.broadcast_days_preset, fake_bot, PresetCB(kind="bc_days", val=7), state, services)
    assert (await state.get_data())["days"] == 7 and await state.get_state() == "Broadcast:text"


async def test_days_other_validates_then_prompts_for_text(services, make_active_client, fake_bot):
    """Свои дни вне границ не записаны, в границах — записаны, диалог ждёт
    текст (переспрос и приглашение с кнопкой — снимки adm.bc.days.bad,
    adm.bc.days.typed)."""
    a = make_active_client("Анна", tg_id=9120)
    state = FakeState()
    await state.set_data({"targets": [a.id], "extend": True})
    await state.set_state("Broadcast:days")
    await _press(admin_h.broadcast_days_preset, fake_bot, PresetCB(kind="bc_days", val=-1), state, services)

    bad = FakeMessage(text="400", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_days(bad, state, services)
    assert "days" not in await state.get_data(), "дни вне границ записаны"

    ok = FakeMessage(text="10", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_days(ok, state, services)
    assert (await state.get_data())["days"] == 10 and await state.get_state() == "Broadcast:text"


async def test_preview_changes_nothing(services, make_active_client, fake_bot):
    """Превью с шапкой и датами (снимок adm.bc.preview.ext.one) подписку не
    трогает: продление — только при отправке."""
    a = make_active_client("Анна", tg_id=9130)
    state = FakeState()
    await state.set_data({"targets": [a.id], "extend": True, "days": 10})
    msg = FakeMessage(text="Спасибо за терпение", html_text="Спасибо за терпение",
                      chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await admin_h.broadcast_receive(msg, state, services)
    assert services.db.get_client(a.id).period_end == a.period_end, "превью продлило подписку"


async def test_send_extends_first_then_delivers_personal_headers(services, make_active_client,
                                                                 fake_bot):
    """Продление — до рассылки и всем отмеченным; держатель переданного
    устройства объявление с продлением не получает. Шапки у каждого свои,
    бессрочному — без строки о продлении, истёкшему — «с текущей даты», отчёт
    с оговоркой про бессрочного — снимок adm.bc.send.ext.marks."""
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
    assert {9140, 9141, 9142} <= set(sent), sent
    old = timeutil.parse_iso(a.period_end)
    new_e = services.db.get_client(e.id)
    assert new_e.status == "active", "истёкшая подписка не ожила"
    assert timeutil.parse_iso(services.db.get_client(a.id).period_end) == old + datetime.timedelta(days=10)
    assert services.db.get_client(u.id).period_end is None
    assert (await state.get_data()) == {}
