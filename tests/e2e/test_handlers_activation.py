"""E2E: активация по коду (_try_activate) — маршрутизация F… → друг, иначе клиент.

Покрывает вход нового друга (роль invited) и активацию клиентского инвайта:
что записано в БД, уведомления хозяину/админу и отказ админу, который зашёл
по коду друга. Тексты и кнопки экранов активации — в эталонах
tests/screens/{client,guest}.txt.
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
    """Друг активировал код — устройство закреплено за ним, хозяин узнал об
    этом. Не запишется держатель — друг увидит гостевую панель, а устройства
    у него не будет; не уйдёт уведомление — хозяин не узнает, что код принят."""
    owner = make_active_client(tg_id=8200, name="Хозяин")
    dc, code = _friendly_device(services, owner.id)
    msg = FakeMessage(text=f"/start {code}", chat_id=98200, user_id=98200,
                      username="guest", bot=fake_bot)
    await client_h._try_activate(msg, services, code)
    dev = services.db.get_device(dc.device_id)
    assert dev.friend_tg_id == 98200
    # хозяину ушло уведомление, что друг подключился
    assert any(r[0] == "send_message" and r[1] == 8200 for r in fake_bot.records)


async def test_activate_friend_code_by_admin_is_refused(services, fake_bot, make_active_client):
    """Админ открыл чужой код друга — отказ, а не гостевой профиль поверх
    админского."""
    owner = make_active_client(tg_id=8202)
    _, code = _friendly_device(services, owner.id)
    msg = FakeMessage(chat_id=config.ADMIN_ID, user_id=config.ADMIN_ID, bot=fake_bot)
    await client_h._try_activate(msg, services, code)
    from awgbot.bot import texts
    assert any(s[0] == "answer" and s[1] == texts.FRIEND_ALREADY_USER for s in msg.sent)


# ── клиент (инвайт) ──────────────────────────────────────────────────────────
async def test_activate_client_invite_happy(services, fake_bot):
    """Инвайт принят — профиль активен и админ получил уведомление. Без
    записи в БД человек увидит «доступ открыт», а бот его не узнает."""
    services.ensure_admin_client()
    created = services.create_client("Новый", 3, "year", traffic_limit=0)
    msg = FakeMessage(text=f"/start {created.invite_code}", chat_id=8300, user_id=8300,
                      username="newbie", bot=fake_bot)
    await client_h._try_activate(msg, services, created.invite_code)
    fresh = services.db.get_client_by_tg(8300)
    assert fresh is not None and fresh.activation_status == "active"
    # админу — уведомление об активации
    assert any(r[0] == "send_message" and r[1] == config.ADMIN_ID for r in fake_bot.records)
