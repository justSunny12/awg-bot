"""E2E: reply-команды (handlers/reply_commands.py) — «Скрыть» и «Отмена»."""
import pytest

from awgbot.bot.handlers import reply_commands as rc
from awgbot.bot.callbacks import HideCB
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
