"""E2E: активация по коду (_try_activate) — маршрутизация F… → друг, иначе клиент.

Покрывает вход нового друга (роль invited) и активацию клиентского инвайта:
успех, невалидный код, «уже пользователь», плюс уведомления хозяину/админу.
"""
import pytest

from awgbot.bot.handlers import client as client_h
from awgbot.core import config
from tests.conftest import FakeMessage

pytestmark = pytest.mark.e2e


def _friendly_device(services, owner_id):
    dc = services.add_device(owner_id, "d")
    code = services.make_device_friendly(dc.device_id)
    return dc, code


# ── друг (код F…) ────────────────────────────────────────────────────────────
async def test_activate_friend_code_happy(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8200, name="Хозяин")
    dc, code = _friendly_device(services, owner.id)
    msg = FakeMessage(text=f"/start {code}", chat_id=98200, user_id=98200,
                      username="guest", bot=fake_bot)
    await client_h._try_activate(msg, services, code)
    # друг получил подтверждение и гостевую панель
    assert any(s[0] == "answer" for s in msg.sent)
    dev = services.db.get_device(dc.device_id)
    assert dev.friend_tg_id == 98200
    # хозяину ушло уведомление, что друг подключился
    assert any(r[0] == "send_message" and r[1] == 8200 for r in fake_bot.records)


async def test_activate_friend_code_invalid(services, fake_bot):
    msg = FakeMessage(text="/start Fbad", chat_id=98201, user_id=98201, bot=fake_bot)
    await client_h._try_activate(msg, services, "Fbad")
    from awgbot.bot import texts
    assert any(s[0] == "answer" and s[1] == texts.ACTIVATION_INVALID for s in msg.sent)


async def test_activate_friend_code_already_user(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8202)
    other = make_active_client(tg_id=98202)                 # уже клиент
    _, code = _friendly_device(services, owner.id)
    msg = FakeMessage(chat_id=98202, user_id=98202, bot=fake_bot)
    await client_h._try_activate(msg, services, code)
    from awgbot.bot import texts
    assert any(s[0] == "answer" and s[1] == texts.FRIEND_ALREADY_USER for s in msg.sent)


# ── клиент (инвайт) ──────────────────────────────────────────────────────────
async def test_activate_client_invite_happy(services, fake_bot):
    services.ensure_admin_client()
    created = services.create_client("Новый", 3, "year", traffic_limit=0)
    msg = FakeMessage(text=f"/start {created.invite_code}", chat_id=8300, user_id=8300,
                      username="newbie", bot=fake_bot)
    await client_h._try_activate(msg, services, created.invite_code)
    from awgbot.bot import texts
    assert any(s[0] == "answer" and s[1] == texts.ACTIVATION_OK for s in msg.sent)
    fresh = services.db.get_client_by_tg(8300)
    assert fresh is not None and fresh.activation_status == "active"
    # админу — уведомление об активации
    assert any(r[0] == "send_message" and r[1] == config.ADMIN_ID for r in fake_bot.records)


async def test_activate_client_invite_invalid(services, fake_bot):
    msg = FakeMessage(text="/start Cxxxxxx", chat_id=8301, user_id=8301, bot=fake_bot)
    await client_h._try_activate(msg, services, "Cxxxxxx")
    from awgbot.bot import texts
    assert any(s[0] == "answer" and s[1] == texts.ACTIVATION_INVALID for s in msg.sent)
