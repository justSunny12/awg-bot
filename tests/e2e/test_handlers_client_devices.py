"""E2E: device-callbacks клиента — карточка, передача другу, добавление (FSM), удаление."""
import pytest

from awgbot.bot.handlers import client as client_h
from awgbot.bot.callbacks import DeviceCB
from awgbot.core.enums import FriendStatus
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


def _cb_with_nav(bot, uid, data=""):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(data=data, message=nav, user_id=uid, bot=bot), nav


# ── передача другу (make friendly → инвайт-код) ──────────────────────────────
async def test_device_transfer_makes_friendly(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=63)
    dc = services.add_device(client.id, "ДляДруга")
    cb, nav = _cb_with_nav(fake_bot, 63)
    await client_h.device_transfer_do(cb, DeviceCB(action="transfer_yes", device_id=dc.device_id),
                                      client, services)
    dev = services.db.get_device(dc.device_id)
    assert dev.friend_code and dev.friend_status == FriendStatus.PENDING
    # прислано сообщение-инвайт со ссылкой на бота
    assert any("test_bot" in s[1] for s in nav.sent if s[0] == "answer")


# ── добавление устройства для себя (FSM: for_whom → name → traffic) ──────────
async def test_add_device_self_full_fsm(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=64, device_limit=3)
    state = FakeState()
    cb, nav = _cb_with_nav(fake_bot, 64)
    await client_h.device_add_self(cb, client, services, state)
    assert (await state.get_data())["for_friend"] is False

    msg = lambda text: FakeMessage(text=text, chat_id=64, user_id=64, bot=fake_bot)
    await client_h.device_add_name(msg("Ноут"), client, services, state)
    assert (await state.get_data())["dev_name"] == "Ноут"
    await client_h.device_add_traffic(msg("50"), client, services, state)

    devs = services.db.list_devices(client.id)
    assert any(d.name == "Ноут" for d in devs)            # устройство создано


async def test_add_device_empty_name_reprompts(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=65)
    state = FakeState()
    await state.update_data(for_friend=False)
    msg = FakeMessage(text="   ", chat_id=65, user_id=65, bot=fake_bot)
    await client_h.device_add_name(msg, client, services, state)
    assert "dev_name" not in await state.get_data()       # пустое имя не принято
    assert any("пуст" in s[1].lower() for s in msg.sent)


