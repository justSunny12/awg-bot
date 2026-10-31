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
    """Гость: роль invited, в client — его собственный
    гостевой профиль; имя подтягивается из Telegram при первом сообщении."""
    owner = make_active_client(tg_id=5001)
    dc = services.add_device(owner.id, "d")
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=9001)
    assert res.ok and res.holder.is_guest and res.holder.name == "Друг"
    mw = AccessMiddleware(services.db)
    result, data = await _run(mw, uid=9001, text="меню")
    assert result == "HANDLED"
    assert data["role"] == "invited" and data["client"].is_guest
    assert data["client"].tg_id == 9001
    assert data["client"].name == "Друг", "профильное имя гостя — раз, при рождении"
    assert data["client"].tg_name == "U", "имя аккаунта не взято из Telegram"
    assert services.db.list_held_devices(data["client"].id)[0].client_id == owner.id


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


async def test_group_and_channel_events_are_dropped(db):
    """Бот работает только в личке: экраны и файлы конфигураций в группу
    уходить не должны, даже если написал админ."""
    from aiogram.types import Chat
    mw = AccessMiddleware(db)

    async def handler(event, data):
        return "HANDLED"

    user = User.model_construct(id=config.ADMIN_ID, is_bot=False, first_name="A")
    for kind in ("group", "supergroup", "channel"):
        chat = Chat.model_construct(id=-100, type=kind)
        assert await mw(handler, _msg(), {"event_from_user": user, "event_chat": chat}) is None, kind
    chat = Chat.model_construct(id=config.ADMIN_ID, type="private")
    assert await mw(handler, _msg(), {"event_from_user": user, "event_chat": chat}) == "HANDLED"


# ── недостающие ветки охраны ─────────────────────────────────────────────────
# Цена ошибки: не активированный профиль или служебная строка с совпавшим
# tg_id получают экраны пользователя; посторонний колбэк получает ответ (бот
# подтверждает своё существование) или стоит запросов в БД на каждый апдейт.

from awgbot.core import access_cache                              # noqa: E402
from tests.conftest import FakeCallback                           # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache():
    access_cache.invalidate_all()
    yield
    access_cache.invalidate_all()


def _set_row(db, cid, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    with db._tx() as cur:
        cur.execute(f"UPDATE clients SET {sets} WHERE id = ?", (*cols.values(), cid))


async def _cb(mw, uid, data="menu"):
    seen = {}

    async def handler(event, d):
        seen.update(d)
        return "HANDLED"
    cb = FakeCallback(data=data, user_id=uid)
    user = User.model_construct(id=uid, is_bot=False, first_name="U")
    out = await mw(handler, cb, {"event_from_user": user})
    return out, cb, (seen if out == "HANDLED" else None)


async def test_a_pending_profile_with_the_tg_id_is_a_stranger(services, make_active_client):
    """Строка с tg_id, но не ACTIVE: текст — молчание, кнопка — молча, /start —
    активация (а не меню пользователя)."""
    c = make_active_client(tg_id=5100)
    _set_row(services.db, c.id, activation_status="pending")
    mw = AccessMiddleware(services.db)
    assert await _run(mw, uid=5100, text="меню") == (None, None)
    out, cb, _ = await _cb(mw, 5100)
    assert out is None and cb.answers == [], "посторонний колбэк получил ответ"
    result, data = await _run(mw, uid=5100, text="/start")
    assert result == "HANDLED" and data["role"] == "activation" and data["client"] is None


async def test_a_service_row_with_the_tg_id_is_a_stranger(services, make_active_client):
    c = make_active_client(tg_id=5101)
    _set_row(services.db, c.id, is_service=1)
    mw = AccessMiddleware(services.db)
    assert await _run(mw, uid=5101, text="меню") == (None, None)
    out, cb, _ = await _cb(mw, 5101)
    assert out is None and cb.answers == []
    result, data = await _run(mw, uid=5101, text="/start")
    assert data["role"] == "activation"


async def test_a_stranger_callback_is_silent_and_negatively_cached(db, monkeypatch):
    """Колбэк постороннего: None, всплывашки нет; второй апдейт — без SQL."""
    calls = []
    orig = db.get_client_by_tg
    monkeypatch.setattr(db, "get_client_by_tg", lambda uid: (calls.append(uid), orig(uid))[1])
    mw = AccessMiddleware(db)
    for _ in range(2):
        out, cb, _ = await _cb(mw, 8889)
        assert out is None and cb.answers == []
    assert calls == [8889], "второй колбэк постороннего снова пошёл в БД"


async def test_start_with_the_bot_name_and_code_from_a_stranger_is_activation(db):
    mw = AccessMiddleware(db)
    for text in ("/start@AwgTestBot", "/start@AwgTestBot CabcABC12345", "/code CabcABC12345",
                 "/code@AwgTestBot CabcABC12345"):
        result, data = await _run(mw, uid=8890, text=text)
        assert result == "HANDLED" and data["role"] == "activation", text
    for text in ("/startx", "/menu", "start", "/codes 1"):
        assert await _run(mw, uid=8890, text=text) == (None, None), text


async def test_a_stranger_message_without_text_is_dropped(db):
    """Фото/стикер без подписи от постороннего: event.text пуст — молчание."""
    mw = AccessMiddleware(db)
    assert await _run(mw, uid=8891, text=None) == (None, None)
