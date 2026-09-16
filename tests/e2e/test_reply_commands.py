"""E2E: reply-команды (handlers/reply_commands.py) — «Скрыть» и «Отмена»."""
import pytest

from awgbot.bot.handlers import reply_commands as rc
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


async def test_on_hide_deletes_message(services, fake_bot):
    nav = FakeMessage(chat_id=700, user_id=700, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=700, bot=fake_bot)
    await rc.on_hide(cb)
    assert any(r[0] == "delete" for r in fake_bot.records)
    assert cb.answers


async def test_on_cancel_clears_state_and_shows_menu(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=701)
    cl = services.db.get_client(client.id)
    st = FakeState()
    await st.update_data(dev_name="in-progress")           # незавершённый диалог
    m = FakeMessage(text="✖️ Отмена", chat_id=701, user_id=701, bot=fake_bot)
    await rc.on_cancel(m, st, services, role="client", client=cl)
    assert await st.get_data() == {}                        # FSM сброшен (clear)
    assert any(s[0] == "answer" and s[1] == "Отменено." for s in m.sent)


async def test_on_cancel_names_the_dialog(services, fake_bot, make_active_client):
    """«Отменено.» без уточнения не говорило, какой из диалогов прерван — а
    их у админа десяток. Финишер называет диалог по группе состояний."""
    from awgbot.bot.states import AddDevice, Broadcast, PauseDays
    from awgbot.bot import texts
    client = make_active_client(tg_id=702)
    cl = services.db.get_client(client.id)
    for st_obj, expected in ((AddDevice.name, "Добавление устройства отменено."),
                             (PauseDays.value, "Приостановка подписки отменена."),
                             (Broadcast.days, "Объявление отменено.")):
        st = FakeState()
        await st.set_state(st_obj)
        m = FakeMessage(text="✖️ Отмена", chat_id=702, user_id=702, bot=fake_bot)
        await rc.on_cancel(m, st, services, role="client", client=cl)
        assert any(s[0] == "answer" and s[1] == expected for s in m.sent), (expected, m.sent)
        assert await st.get_state() is None
    assert texts.cancelled(None) == "Отменено."
