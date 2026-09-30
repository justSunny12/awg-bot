"""E2E: продление подписки из карточки профиля и из списка «Истекают».

Экран продления: сроки кнопками, тумблер «Сохранить остаток» (только при
остатке, по умолчанию ✅; состояние — в колбэке срока), отмена. Срок —
сразу, без второго вопроса: след двумя строками и следом экран, откуда
пришли (карточка / «Истекают»).

Цена ошибки: тумблер по умолчанию снят — у человека сгорают оплаченные дни;
вопрос после срока вернулся — лишний шаг на каждом продлении; возврат не туда —
админ, продлевающий подряд из списка истекающих, каждый раз ищет его заново.
"""
import datetime

import pytest

from awgbot.bot.callbacks import ClientCB, Menu, PeriodCB
from awgbot.bot.handlers import admin as admin_h
from awgbot.core import config
from awgbot.util import timeutil
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _admin_cb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _buttons(nav):
    markup = [s for s in nav.sent if s[0] == "edit_text"][-1][2]
    return [b for row in markup.inline_keyboard for b in row]


async def _open(services, bot, client_id, state=None, action="extend"):
    cb, nav = _admin_cb(bot)
    await admin_h.extend_start(cb, ClientCB(action=action, client_id=client_id), services,
                               state or FakeState())
    return cb, nav


async def test_extend_screen_offers_periods_keep_toggle_on_by_default_and_cancel_to_card(
        services, fake_bot, make_active_client):
    """«⏱ <b>Продление:</b> Имя» ⏎ «Сейчас до … · осталось …»; сроки, «∞»,
    «✅ Сохранить остаток» (по умолчанию включён), «⬅️ Отмена» — в карточку."""
    client = make_active_client(tg_id=7200, period_kind="year")
    _, nav = await _open(services, fake_bot, client.id)
    text, labels = last_screen(nav)
    end = timeutil.parse_iso(services.db.get_client(client.id).period_end)
    assert text == (f"⏱ <b>Продление:</b> {client.name}\n"
                    f"Сейчас до {timeutil.fmt_end_ui(end)} · осталось {timeutil.remaining_brief(end)}"), text
    assert labels == ["День", "Неделя", "Месяц", "Год", "∞", "✅ Сохранить остаток", "⬅️ Отмена"], labels
    btns = _buttons(nav)
    assert all(PeriodCB.unpack(b.callback_data).keep == 1 for b in btns[:4]), \
        "сроки при включённом тумблере должны нести keep=1"
    assert btns[-1].callback_data == ClientCB(action="open", client_id=client.id).pack()


async def test_keep_toggle_switches_off_and_back(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7201, period_kind="year")
    _, nav = await _open(services, fake_bot, client.id)
    toggle = [b for b in _buttons(nav) if "Сохранить остаток" in b.text][0]
    cb, nav2 = _admin_cb(fake_bot)
    await admin_h.extend_keep_toggle(cb, PeriodCB.unpack(toggle.callback_data), services, FakeState())
    _, labels = last_screen(nav2)
    assert "☑️ Сохранить остаток" in labels, labels
    assert all(PeriodCB.unpack(b.callback_data).keep == 0 for b in _buttons(nav2)[:4]), \
        "снятый тумблер не дошёл до кнопок сроков"
    toggle2 = [b for b in _buttons(nav2) if "Сохранить остаток" in b.text][0]
    cb3, nav3 = _admin_cb(fake_bot)
    await admin_h.extend_keep_toggle(cb3, PeriodCB.unpack(toggle2.callback_data), services, FakeState())
    assert "✅ Сохранить остаток" in last_screen(nav3)[1]


async def test_no_keep_toggle_without_remainder(services, fake_bot, make_active_client):
    """Нечего сохранять (истекла или бессрочная) — тумблера нет."""
    client = make_active_client(tg_id=7202, period_kind="never")
    _, nav = await _open(services, fake_bot, client.id)
    text, labels = last_screen(nav)
    assert text.endswith("Сейчас: бессрочная"), text
    assert not any("остаток" in l for l in labels), labels


@pytest.mark.parametrize("keep", [1, 0])
async def test_period_applies_at_once_leaves_a_two_line_note_and_returns_to_the_card(
        services, fake_bot, make_active_client, keep):
    """Срок — сразу: остаток по тумблеру (с ним — от прежнего конца, без — от
    сейчас), след двумя строками, следом — карточка профиля живым меню."""
    client = make_active_client(tg_id=7203, period_kind="year")
    old_end = timeutil.parse_iso(services.db.get_client(client.id).period_end)
    cb, nav = _admin_cb(fake_bot)
    state = FakeState()
    await admin_h.extend_period_chosen(
        cb, PeriodCB(kind="month", ctx="extend", ref=client.id, keep=keep), services, state)
    fresh = services.db.get_client(client.id)
    new_end = timeutil.parse_iso(fresh.period_end)
    base = old_end if keep else timeutil.now()
    assert abs((new_end - timeutil.add_period(base, "month")).total_seconds()) < 120, \
        f"остаток {'не сохранён' if keep else 'сохранён без спроса'}: {new_end}"
    assert fresh.period_kind == "month"
    note = [s[1] for s in nav.sent if s[0] == "edit_text"][-1]
    lines = note.split("\n")
    assert lines[0] == f"✅ {client.name}: подписка продлена на месяц,", note
    assert lines[1].startswith(f"→ {timeutil.fmt_end_ui(new_end)}"), note
    shown = [s for s in nav.sent if s[0] == "answer"]
    assert shown and shown[-1][1].startswith("👤 "), "после продления — не карточка профиля"
    assert services.db.get_nav_message_id(ADMIN) != nav.message_id, "след остался живым меню"
    assert await state.get_data() == {}


async def test_never_ignores_keep_and_makes_it_unlimited(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7204, period_kind="year")
    cb, nav = _admin_cb(fake_bot)
    await admin_h.extend_period_chosen(
        cb, PeriodCB(kind="never", ctx="extend", ref=client.id, keep=1), services, FakeState())
    fresh = services.db.get_client(client.id)
    assert fresh.period_kind == "never" and fresh.period_end is None
    assert [s[1] for s in nav.sent if s[0] == "edit_text"][-1] == \
        f"✅ {client.name}: подписка теперь бессрочная"


async def test_extend_from_expired_reactivates(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7205, period_kind="year")
    from awgbot.core.blocks import ClientBlock
    services.db.update_client_fields(client.id, status="expired")
    services._client_set_block(client.id, ClientBlock.EXPIRY)
    cb, nav = _admin_cb(fake_bot)
    await admin_h.extend_period_chosen(
        cb, PeriodCB(kind="month", ctx="extend", ref=client.id, keep=0), services, FakeState())
    fresh = services.db.get_client(client.id)
    assert fresh.status == "active"
    assert int(fresh.block_reason) & int(ClientBlock.EXPIRY) == 0, "причина EXPIRY не снята"


async def test_grace_debt_warns_and_hides_periods_shorter_than_it(services, fake_bot, make_active_client):
    """Брал отсрочку — строка «⚠️ Брал отсрочку на N дн. — вычтется», сроки
    короче долга не предлагаются (продлить на день при долге в неделю — в минус)."""
    client = make_active_client(tg_id=7206, period_kind="year")
    services.db.update_client_fields(client.id, grace_pending_cut=7 * 86400)
    _, nav = await _open(services, fake_bot, client.id)
    text, labels = last_screen(nav)
    assert text.endswith("⚠️ Брал отсрочку на 7 дн. — вычтется"), text
    assert "День" not in labels and "Неделя" not in labels and "Месяц" in labels, labels


def _expiring(services, make_active_client, name, tg_id, days):
    c = make_active_client(name, tg_id=tg_id)
    now = timeutil.now()
    services.db.update_client_fields(c.id, period_start=timeutil.to_iso(now - datetime.timedelta(days=20)),
                                     period_end=timeutil.to_iso(now + datetime.timedelta(days=days)),
                                     period_kind="month")
    return services.db.get_client(c.id)


async def test_extend_from_the_expiring_list_returns_to_it_while_it_is_not_empty(
        services, fake_bot, make_active_client):
    """«⏱ Имя» в «Истекают»: отмена — обратно в список; после продления — снова
    список, пока в нём кто-то есть; последний продлён — карточка."""
    a = _expiring(services, make_active_client, "Аня", 7210, 2)
    b = _expiring(services, make_active_client, "Боря", 7211, 3)

    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_expiring(cb, services, FakeState())
    buttons = _buttons(nav)
    assert [x.text for x in buttons] == ["⏱ Аня", "⏱ Боря", "⬅️ В меню"], [x.text for x in buttons]

    state = FakeState()
    cb2, nav2 = _admin_cb(fake_bot)
    await admin_h.extend_start(cb2, ClientCB.unpack(buttons[0].callback_data), services, state)
    assert _buttons(nav2)[-1].callback_data == Menu(action="expiring").pack(), \
        "отмена продления из списка ведёт не в список"
    period = [x for x in _buttons(nav2) if x.text == "Месяц"][0]
    await admin_h.extend_period_chosen(cb2, PeriodCB.unpack(period.callback_data), services, state)
    after = [s[1] for s in nav2.sent if s[0] == "answer"]
    assert after and after[-1].startswith("⏳ <b>Истекают:</b> 1"), after
    assert "Боря" in after[-1] and "Аня" not in after[-1]

    state2 = FakeState()
    cb3, nav3 = _admin_cb(fake_bot)
    await admin_h.extend_start(cb3, ClientCB(action="extend_exp", client_id=b.id), services, state2)
    period = [x for x in _buttons(nav3) if x.text == "Месяц"][0]
    await admin_h.extend_period_chosen(cb3, PeriodCB.unpack(period.callback_data), services, state2)
    last = [s[1] for s in nav3.sent if s[0] == "answer"]
    assert last and last[-1].startswith("👤 "), "список опустел — должна прийти карточка"
    assert a.id != b.id
