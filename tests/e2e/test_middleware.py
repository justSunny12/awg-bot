"""E2E: AccessMiddleware — гвард на входе (роль/дроп) по whitelist."""
import pytest
from aiogram.types import Chat, Message, User

from awgbot.bot.middleware import AccessMiddleware
from awgbot.core import config

pytestmark = pytest.mark.e2e


def _msg(text="/start"):
    return Message.model_construct(message_id=1, text=text,
                                   chat=Chat.model_construct(id=7, type="private"))


async def _run(mw, uid, text="/start", username=None):
    """Прогнать событие через middleware; вернуть (result, data)."""
    captured = {}

    async def handler(event, data):
        captured.update(data)
        return "HANDLED"

    user = User.model_construct(id=uid, is_bot=False, first_name="U", username=username)
    data = {"event_from_user": user}
    result = await mw(handler, _msg(text), data)
    return result, (captured if result == "HANDLED" else None)


async def test_admin_gets_admin_role(db):
    mw = AccessMiddleware(db)
    result, data = await _run(mw, uid=config.ADMIN_ID)
    assert result == "HANDLED" and data["role"] == "admin"


async def test_active_client_gets_client_role(db, make_active_client):
    client = make_active_client(tg_id=5000)
    mw = AccessMiddleware(db)
    result, data = await _run(mw, uid=5000, text="привет")
    assert result == "HANDLED"
    assert data["role"] == "client" and data["client"].id == client.id


async def test_invited_friend_role(services, make_active_client):
    owner = make_active_client(tg_id=5001)
    dc = services.add_device(owner.id, "d")
    services.db.set_device_friend(dc.device_id, friend_tg_id=9001,
                                  friend_code="Fabc", friend_status="active")
    mw = AccessMiddleware(services.db)
    result, data = await _run(mw, uid=9001, text="меню")
    assert result == "HANDLED"
    assert data["role"] == "invited" and data["device"].id == dc.device_id
    assert data["client"].id == owner.id                 # хозяин прокинут


async def test_stranger_start_is_activation(db):
    mw = AccessMiddleware(db)
    result, data = await _run(mw, uid=8888, text="/start CabcABC12345")
    assert result == "HANDLED" and data["role"] == "activation"


async def test_stranger_code_is_activation(db):
    mw = AccessMiddleware(db)
    result, data = await _run(mw, uid=8888, text="/code CabcABC12345")
    assert result == "HANDLED" and data["role"] == "activation"


async def test_stranger_random_text_dropped(db):
    mw = AccessMiddleware(db)
    result, data = await _run(mw, uid=8888, text="просто текст")
    assert result is None and data is None                # молчаливый дроп


async def test_no_user_dropped(db):
    mw = AccessMiddleware(db)

    async def handler(event, data):
        return "HANDLED"

    result = await mw(handler, _msg(), {})               # нет event_from_user
    assert result is None
