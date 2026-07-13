"""E2E: полный роутер друга (handlers/friend.py) — список, обновление, карточка,
выдача конфигов, помощь по платформе.
"""
import pytest

from awgbot.bot.handlers import friend as fh
from awgbot.bot.callbacks import FriendCB, HelpCB
from tests.conftest import FakeCallback, FakeMessage

pytestmark = pytest.mark.e2e


def _fcb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _befriend(services, owner_id, friend_tg, name="d"):
    dc = services.add_device(owner_id, name)
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=friend_tg)
    return dc


async def test_friend_list_single_multi_none(services, fake_bot, make_active_client):
    a = make_active_client(tg_id=6100)
    _befriend(services, a.id, 96100, "One")
    cb, nav = _fcb(fake_bot, 96100)
    await fh.friend_list(cb, services)                       # одно → карточка
    assert any(s[0] == "edit_text" for s in nav.sent)
    b = make_active_client(tg_id=6101)
    _befriend(services, b.id, 96100, "Two")
    cb2, nav2 = _fcb(fake_bot, 96100)
    await fh.friend_list(cb2, services)                      # несколько → список
    assert any(s[0] == "edit_text" for s in nav2.sent)
    cb3, nav3 = _fcb(fake_bot, 96199)
    await fh.friend_list(cb3, services)                      # ни одного → alert
    assert cb3.answers[-1][1] is True


async def test_friend_refresh_variants(services, fake_bot, make_active_client):
    a = make_active_client(tg_id=6102)
    dc = _befriend(services, a.id, 96102)
    cb, nav = _fcb(fake_bot, 96102)
    await fh.friend_refresh(cb, FriendCB(action="refresh", device_id=dc.device_id), services)
    assert cb.answers[-1][0] == "Обновлено"
    cb2, nav2 = _fcb(fake_bot, 96102)
    await fh.friend_refresh(cb2, FriendCB(action="refresh", device_id=0), services)  # безадресная → панель
    assert any(s[0] == "edit_text" for s in nav2.sent)
    cb3, nav3 = _fcb(fake_bot, 96103)                        # нет устройств
    await fh.friend_refresh(cb3, FriendCB(action="refresh", device_id=0), services)
    assert cb3.answers[-1][1] is True


async def test_friend_connect_menu_and_gen(services, fake_bot, make_active_client):
    a = make_active_client(tg_id=6104)
    dc = _befriend(services, a.id, 96104)
    cb, nav = _fcb(fake_bot, 96104)
    await fh.friend_connect_menu(cb, FriendCB(action="connect_menu", device_id=dc.device_id), services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _fcb(fake_bot, 96104)
    await fh.friend_gen_link(cb2, FriendCB(action="gen_link", device_id=dc.device_id), services)
    assert any(s[0] == "answer" for s in nav2.sent)
    cb3, nav3 = _fcb(fake_bot, 96104)
    await fh.friend_gen_qr(cb3, FriendCB(action="gen_qr", device_id=dc.device_id), services)
    assert any(s[0] == "animation" for s in nav3.sent)
    cb4, nav4 = _fcb(fake_bot, 96104)
    await fh.friend_gen_file(cb4, FriendCB(action="gen_file", device_id=dc.device_id), services)
    assert any(s[0] == "document" for s in nav4.sent)


async def test_friend_gen_foreign_guarded(services, fake_bot, make_active_client):
    a = make_active_client(tg_id=6105)
    dc = _befriend(services, a.id, 96105)
    cb, nav = _fcb(fake_bot, 96106)                          # чужой tg
    await fh.friend_gen_link(cb, FriendCB(action="gen_link", device_id=dc.device_id), services)
    assert cb.answers[-1][1] is True


async def test_friend_help_and_platform(services, fake_bot, make_active_client):
    a = make_active_client(tg_id=6107)
    _befriend(services, a.id, 96107)
    cb, nav = _fcb(fake_bot, 96107)
    await fh.friend_help(cb)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _fcb(fake_bot, 96107)
    await fh.friend_help_platform(cb2, HelpCB(platform="android"))
    assert any(s[0] == "edit_text" for s in nav2.sent)
