"""E2E: device-callbacks клиента — передача другу, добавление (FSM).

Тексты, подписи и колбэки этих экранов — в эталоне tests/screens/client.txt;
здесь — что записано в БД и в память диалога и что убрано из чата.
"""
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
    """«Другу» заводит код и ставит устройство в ожидание друга: без кода
    приглашение вести некуда."""
    client = make_active_client(tg_id=63)
    dc = services.add_device(client.id, "ДляДруга")
    cb, nav = _cb_with_nav(fake_bot, 63)
    await client_h.device_transfer_do(cb, DeviceCB(action="transfer_yes", device_id=dc.device_id),
                                      client, services)
    dev = services.db.get_device(dc.device_id)
    assert dev.friend_code and dev.friend_status == FriendStatus.PENDING


# ── добавление устройства себе: одно поле — имя, затем экран с выдачей ─────
async def test_add_device_self_full_fsm(services, make_active_client, fake_bot):
    """Себе — один ввод: имя. Без вопроса о лимите (он ставится в карточке);
    после имени — устройство в БД без своего лимита, диалог закрыт.
    Приглашение и ввод убраны из чата. Итог с лимитом профиля — снимок
    cl.add.name.done.capped."""
    client = make_active_client(tg_id=64, device_limit=3, traffic_limit=100 * 1024 ** 3)
    state = FakeState()
    cb, nav = _cb_with_nav(fake_bot, 64)
    await client_h.device_add_start(cb, DeviceCB(action="add"), client, services, state)
    assert (await state.get_data())["for_friend"] is False

    typed = FakeMessage(text="Ноут", chat_id=64, user_id=64, bot=fake_bot)
    await client_h.device_add_name(typed, client, services, state)
    devs = services.db.list_devices(client.id)
    assert [d.name for d in devs] == ["Ноут"] and devs[0].traffic_limit == 0
    assert await state.get_state() is None, "после имени спрашивают ещё что-то"
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert {nav.message_id, typed.message_id} <= deleted, "приглашение или ввод остались в чате"


async def test_add_device_empty_name_reprompts(services, make_active_client, fake_bot):
    """Пустое имя не принимается: диалог не продвигается дальше."""
    client = make_active_client(tg_id=65)
    state = FakeState()
    await state.update_data(for_friend=False)
    msg = FakeMessage(text="   ", chat_id=65, user_id=65, bot=fake_bot)
    await client_h.device_add_name(msg, client, services, state)
    assert "dev_name" not in await state.get_data()       # пустое имя не принято
    assert services.db.list_devices(client.id) == [], "устройство с пустым именем создано"
