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


# ── добавление устройства себе: одно поле — имя, затем экран с выдачей ─────
async def test_add_device_self_full_fsm(services, make_active_client, fake_bot):
    """Себе — один ввод: имя. Без вопроса о лимите (он ставится в карточке);
    после имени — экран с рядом выдачи. Приглашение и ввод убраны."""
    from awgbot.bot.callbacks import CancelCB
    client = make_active_client(tg_id=64, device_limit=3, traffic_limit=100 * 1024 ** 3)
    state = FakeState()
    cb, nav = _cb_with_nav(fake_bot, 64)
    await client_h.device_add_start(cb, DeviceCB(action="add"), client, services, state)
    prompt = [s for s in nav.sent if s[0] == "edit_text"][-1]
    assert prompt[1] == "➕ <b>Новое устройство</b> · 0 из 3\nКак назвать? Например: «iPhone»"
    buttons = [b for row in prompt[2].inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["👤 Это для друга", "✖️ Отмена"]
    assert CancelCB.unpack(buttons[1].callback_data).kind == "main"
    assert (await state.get_data())["for_friend"] is False

    typed = FakeMessage(text="Ноут", chat_id=64, user_id=64, bot=fake_bot)
    await client_h.device_add_name(typed, client, services, state)
    devs = services.db.list_devices(client.id)
    assert [d.name for d in devs] == ["Ноут"] and devs[0].traffic_limit == 0
    assert await state.get_state() is None, "после имени спрашивают ещё что-то"
    shown = [s for s in typed.sent if s[0] == "answer"]
    assert shown[-1][1] == "✅ Ноут создано · трафик в пределах 100 ГБ профиля"
    rows = [[b.text for b in r] for r in shown[-1][2].inline_keyboard]
    assert rows == [["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["❓ Как подключить", "⬅️ В меню"]], rows
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert {nav.message_id, typed.message_id} <= deleted, "приглашение или ввод остались в чате"


async def test_add_device_from_devices_list_cancels_back_to_the_list(
        services, make_active_client, fake_bot):
    """«➕ Устройство» на экране «📱 Устройства»: «✖️ Отмена» возвращает в
    список, откуда пришли, а не на главную. Идём по настоящим кнопкам:
    колбэк берём с экрана списка, отмену — с приглашения."""
    from awgbot.bot.callbacks import CancelCB
    from awgbot.bot.handlers import reply_commands as rc
    client = make_active_client(tg_id=66)
    services.add_device(client.id, "Тел")
    state = FakeState()
    cb, nav = _cb_with_nav(fake_bot, 66)
    await client_h.menu_devices(cb, client, services)
    add = [b for row in nav.sent[-1][2].inline_keyboard for b in row if b.text == "➕ Устройство"][0]
    cb, nav = _cb_with_nav(fake_bot, 66)
    await client_h.device_add_start(cb, DeviceCB.unpack(add.callback_data), client, services, state)
    cancel = [b for row in nav.sent[-1][2].inline_keyboard for b in row if b.text == "✖️ Отмена"][0]
    cb = FakeCallback(message=nav, user_id=66, bot=fake_bot)
    await rc.on_cancel_inline(cb, CancelCB.unpack(cancel.callback_data), state, services,
                              role="client", client=client)
    assert nav.sent[-1][1].startswith("📱 <b>Устройства</b> · 1 из 3"), \
        f"отмена увела не в список, а на: {nav.sent[-1][1]!r}"


async def test_add_device_empty_name_reprompts(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=65)
    state = FakeState()
    await state.update_data(for_friend=False)
    msg = FakeMessage(text="   ", chat_id=65, user_id=65, bot=fake_bot)
    await client_h.device_add_name(msg, client, services, state)
    assert "dev_name" not in await state.get_data()       # пустое имя не принято
    assert any("пуст" in s[1].lower() for s in msg.sent)


