"""E2E: продление подписки из карточки профиля и из списка «Истекают».

Экран продления: сроки кнопками, тумблер «Сохранить остаток» (только при
остатке, по умолчанию ✅; состояние — в колбэке срока), отмена. Срок —
сразу, без второго вопроса: след двумя строками и следом экран, откуда
пришли (карточка / «Истекают»).

Цена ошибки: тумблер по умолчанию снят — у человека сгорают оплаченные дни;
вопрос после срока вернулся — лишний шаг на каждом продлении; возврат не туда —
админ, продлевающий подряд из списка истекающих, каждый раз ищет его заново.

Экраны продления (с остатком, тумблер туда и обратно, истёкшая, бессрочная,
отсрочка с границей в неделю, «∞», итог и возврат в «Истекают») — в эталоне
tests/screens/admin.txt; здесь — срок в БД, живое меню и память диалога.
"""
import datetime

import pytest

from awgbot.bot.callbacks import ClientCB, PeriodCB
from awgbot.bot.handlers import admin as admin_h
from awgbot.core import config
from awgbot.util import timeutil
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _admin_cb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _buttons(nav):
    markup = [s for s in nav.sent if s[0] == "edit_text"][-1][2]
    return [b for row in markup.inline_keyboard for b in row]


@pytest.mark.parametrize("keep", [1, 0])
async def test_period_applies_at_once_leaves_a_two_line_note_and_returns_to_the_card(
        services, fake_bot, make_active_client, keep):
    """Срок — сразу: остаток по тумблеру (с ним — от прежнего конца, без — от
    сейчас), след перестаёт быть живым меню (им становится карточка), память
    диалога пуста. Текст следа и карточка — в эталоне."""
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
    assert services.db.get_nav_message_id(ADMIN) != nav.message_id, "след остался живым меню"
    assert await state.get_data() == {}


async def test_never_ignores_keep_and_makes_it_unlimited(services, fake_bot, make_active_client):
    """«∞» делает подписку бессрочной независимо от тумблера: остаток тут
    некуда сохранять (след — снимок adm.cl.extend.never)."""
    client = make_active_client(tg_id=7204, period_kind="year")
    cb, nav = _admin_cb(fake_bot)
    await admin_h.extend_period_chosen(
        cb, PeriodCB(kind="never", ctx="extend", ref=client.id, keep=1), services, FakeState())
    fresh = services.db.get_client(client.id)
    assert fresh.period_kind == "never" and fresh.period_end is None, "подписка не стала бессрочной"


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


def _expiring(services, make_active_client, name, tg_id, days):
    c = make_active_client(name, tg_id=tg_id)
    now = timeutil.now()
    services.db.update_client_fields(c.id, period_start=timeutil.to_iso(now - datetime.timedelta(days=20)),
                                     period_end=timeutil.to_iso(now + datetime.timedelta(days=days)),
                                     period_kind="month")
    return services.db.get_client(c.id)


async def test_extend_from_the_expiring_list_takes_the_profile_out_of_it(
        services, fake_bot, make_active_client):
    """«⏱ Имя» в «Истекают»: после продления — снова список, пока в нём
    кто-то есть (экран — снимок adm.cl.extend_exp.more): продлённый из
    истекающих ушёл, второй остался. Отмена в список и «последний продлён —
    карточка» — в эталоне."""
    _expiring(services, make_active_client, "Аня", 7210, 2)
    _expiring(services, make_active_client, "Боря", 7211, 3)

    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_expiring(cb, services, FakeState())
    buttons = _buttons(nav)

    state = FakeState()
    cb2, nav2 = _admin_cb(fake_bot)
    await admin_h.extend_start(cb2, ClientCB.unpack(buttons[0].callback_data), services, state)
    period = [x for x in _buttons(nav2) if x.text == "Месяц"][0]
    await admin_h.extend_period_chosen(cb2, PeriodCB.unpack(period.callback_data), services, state)
    left = [c.name for c, _ in services.expiring_subscriptions()]
    assert left == ["Боря"], left
