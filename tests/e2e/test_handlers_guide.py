"""E2E: визард-гайды (handlers/guide.py) — добавление устройства внутри
гайда: что меняется в БД и в диалоге.

Тексты и кнопки шагов, выбор способа (в том числе файл и шаг «Подключаемся»),
«Назад», отказ при лимите, пустое имя — в эталонах tests/screens/client.txt и
guest.txt (cl.guide.*, gst.guide.*).
"""
import pytest

from awgbot.bot.handlers import guide as gh
from awgbot.bot.callbacks import GuideCB
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


async def test_guide_add_device_flow(services, fake_bot, make_active_client):
    """Новое устройство внутри гайда: после имени устройство создано и ввод
    закрыт — иначе следующий текст человека снова станет именем."""
    client = make_active_client(tg_id=6203, device_limit=3)
    cl = services.db.get_client(client.id)
    st = FakeState()
    cb, nav = _cb(fake_bot, 6203)
    await gh.guide_add_device(cb, GuideCB(guide="connect", step=-1), services, cl, st)
    m_name = FakeMessage(text="Дев", chat_id=6203, user_id=6203, bot=fake_bot)
    await gh.guide_add_device_name(m_name, services, cl, st)
    assert any(d.name == "Дев" for d in services.db.list_devices(client.id)), "устройство не создано"
    assert await st.get_state() is None, "ввод имени остался открытым"


async def test_guide_add_device_cancel_returns_to_the_guide_step(services, fake_bot,
                                                                 make_active_client):
    """«✖️ Отмена» на вводе имени внутри гайда закрывает ввод и ничего не
    создаёт и не трогает: человек посреди настройки и возвращается на шаг."""
    from awgbot.bot.handlers import reply_commands as rc
    from awgbot.bot.callbacks import CancelCB
    client = make_active_client(tg_id=6206, device_limit=3)
    services.add_device(client.id, "Старое")
    cl = services.db.get_client(client.id)
    st = FakeState()
    cb, nav = _cb(fake_bot, 6206)
    await gh.guide_add_device(cb, GuideCB(guide="connect_apple", step=-1), services, cl, st)
    cancel = CancelCB.unpack(nav.sent[-1][2].inline_keyboard[0][0].callback_data)
    cb2 = FakeCallback(message=nav, user_id=6206, bot=fake_bot)
    await rc.on_cancel_inline(cb2, cancel, st, services, role="client", client=cl)
    assert await st.get_state() is None and await st.get_data() == {}
    assert [d.name for d in services.db.list_devices(client.id)] == ["Старое"]
