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


# ── карточка устройства ──────────────────────────────────────────────────────
async def test_device_open_own(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=60)
    dc = services.add_device(client.id, "Телефон")
    cb, nav = _cb_with_nav(fake_bot, 60)
    await client_h.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), client, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    assert cb.answers


async def test_device_open_foreign_rejected(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=61)
    other = make_active_client(tg_id=62)
    dc = services.add_device(other.id, "Чужой")          # принадлежит другому клиенту
    cb, nav = _cb_with_nav(fake_bot, 61)
    await client_h.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), client, services)
    assert cb.answers and cb.answers[0][1] is True        # show_alert «не найдено»
    assert not any(s[0] == "edit_text" for s in nav.sent)


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


# ── меню удаления устройства ─────────────────────────────────────────────────
async def test_del_menu_lists_devices(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=66)
    services.add_device(client.id, "d")
    cb, nav = _cb_with_nav(fake_bot, 66)
    await client_h.device_del_menu(cb, client, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    assert cb.answers


async def test_del_menu_without_devices_alerts(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=67)
    cb, nav = _cb_with_nav(fake_bot, 67)
    await client_h.device_del_menu(cb, client, services)
    assert cb.answers and cb.answers[0][1] is True        # show_alert «нет устройств»
