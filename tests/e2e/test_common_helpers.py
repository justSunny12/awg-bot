"""E2E: edge-ветки общих хелперов handlers/common.py — гашение прежнего нав,
drop_message с фолбэком, завершитель по ролям, выдача qr, меню друга."""
import pytest

from awgbot.bot.handlers import common as cm
from tests.conftest import FakeBot, FakeCallback, FakeMessage

pytestmark = pytest.mark.e2e


async def test_dismiss_previous_nav(services, fake_bot):
    services.db.set_nav_message_id(500, 55)
    await cm._dismiss_previous_nav(fake_bot, services, 500, keep_id=None)
    assert any(r[0] == "edit_markup" for r in fake_bot.records)   # прежние кнопки сняты
    fake_bot.records.clear()
    await cm._dismiss_previous_nav(fake_bot, services, 500, keep_id=55)  # то же сообщение → no-op
    assert not any(r[0] == "edit_markup" for r in fake_bot.records)
    await cm._dismiss_previous_nav(fake_bot, services, 999, keep_id=None)  # нет нав → no-op


class _NoDeleteMessage(FakeMessage):
    async def delete(self):
        raise RuntimeError("too old")


async def test_drop_message_falls_back_to_unmark(fake_bot):
    nav = _NoDeleteMessage(chat_id=1, user_id=1, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=1, bot=fake_bot)
    await cm.drop_message(cb)                               # delete падает → снимаем кнопки
    assert any(r[0] == "edit_reply_markup" for r in fake_bot.records)


async def test_content_finisher_roles(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7300)
    nav = FakeMessage(chat_id=7300, user_id=7300, bot=fake_bot)
    await cm.content_finisher(nav, services, "готово", "client")
    assert any(s[0] == "answer" for s in nav.sent)
    nav2 = FakeMessage(chat_id=7301, user_id=7301, bot=fake_bot)
    await cm.content_finisher(nav2, services, "готово", "invited")   # ветка друга
    assert any(s[0] == "answer" for s in nav2.sent)


async def test_send_device_config_qr(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7302)
    dc = services.add_device(client.id, "d")
    dev = services.db.get_device(dc.device_id)
    nav = FakeMessage(chat_id=7302, user_id=7302, bot=fake_bot)
    await cm.send_device_config(nav, services, dev, "qr")
    assert any(s[0] == "animation" for s in nav.sent)


async def test_show_main_menu_invited(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=7303)
    dc = services.add_device(owner.id, "d")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=97303)
    msg = FakeMessage(chat_id=97303, user_id=97303, bot=fake_bot)
    await cm.show_main_menu(msg, services, "invited", None)
    assert any(s[0] == "answer" for s in msg.sent)
