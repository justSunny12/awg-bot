"""E2E: friend-хендлеры (роль invited) — стартовый экран, карточка, защита владения.

Сервисный слой friend покрыт отдельно; здесь — UI-обёртка: панель (одно
устройство → карточка, несколько → список), открытие конкретного устройства,
защита от чужого device_id в callback.
"""
import pytest

from awgbot.bot.handlers import friend as friend_h
from awgbot.bot.callbacks import FriendCB
from tests.conftest import FakeCallback, FakeMessage

pytestmark = pytest.mark.e2e


def _befriend(services, owner_id, friend_tg, name="d"):
    dc = services.add_device(owner_id, name)
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=friend_tg)
    return dc


async def test_panel_payload_single_device_is_card(services, make_active_client):
    owner = make_active_client(tg_id=8100)
    dc = _befriend(services, owner.id, friend_tg=98100, name="Ноут")
    text, markup = await friend_h.friend_panel_payload(services, 98100)
    assert markup is not None
    assert "Ноут" in text                                   # карточка конкретного устройства


async def test_panel_payload_multi_device_is_list(services, make_active_client):
    owner_a = make_active_client(tg_id=8101)
    owner_b = make_active_client(tg_id=8102)
    _befriend(services, owner_a.id, friend_tg=98101, name="A")
    _befriend(services, owner_b.id, friend_tg=98101, name="B")
    text, markup = await friend_h.friend_panel_payload(services, 98101)
    assert markup is not None
    assert "стройств" in text                               # экран выбора устройства


async def test_panel_payload_no_device(services):
    text, markup = await friend_h.friend_panel_payload(services, 98199)
    assert markup is None
    assert "не найден" in text.lower()


async def test_friend_start_renders_panel(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8103)
    _befriend(services, owner.id, friend_tg=98103, name="Планшет")
    msg = FakeMessage(text="/start", chat_id=98103, user_id=98103, bot=fake_bot)
    await friend_h.friend_start(msg, services)
    assert any(s[0] == "answer" and "Планшет" in s[1] for s in msg.sent)


async def test_friend_open_valid(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8104)
    dc = _befriend(services, owner.id, friend_tg=98104, name="Телефон")
    nav = FakeMessage(chat_id=98104, user_id=98104, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=98104, bot=fake_bot)
    await friend_h.friend_open(cb, FriendCB(action="open", device_id=dc.device_id), services)
    assert any(s[0] == "edit_text" and "Телефон" in s[1] for s in nav.sent)
    assert cb.answers


async def test_friend_open_foreign_device_guarded(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8105)
    dc = _befriend(services, owner.id, friend_tg=98105, name="d")
    # ДРУГОЙ друг пытается открыть чужое устройство по его id
    nav = FakeMessage(chat_id=98106, user_id=98106, bot=fake_bot)
    cb = FakeCallback(message=nav, user_id=98106, bot=fake_bot)
    await friend_h.friend_open(cb, FriendCB(action="open", device_id=dc.device_id), services)
    assert cb.answers and cb.answers[-1][1] is True          # show_alert
    assert not any(s[0] == "edit_text" for s in nav.sent)    # карточку не показали
