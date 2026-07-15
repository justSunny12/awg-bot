"""E2E: тела хендлеров (прямой вызов с фейковыми Bot/Message/Callback/State).

Роль/маршрутизацию покрывают smoke (сборка роутеров) и test_middleware (гвард).
Здесь — что хендлер делает: какие сообщения шлёт и как меняет состояние.
"""

import pytest

from awgbot.bot.handlers import client as client_h
from awgbot.core import config
from tests.conftest import FakeBot, FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


# ── активация клиента (/start {код}, /code) ──────────────────────────────────
async def test_try_activate_valid_code(services, fake_bot):
    created = services.create_client("Клиент", 2, "year")
    msg = FakeMessage(text="/start", chat_id=42, user_id=42, bot=fake_bot)
    await client_h._try_activate(msg, services, created.invite_code)
    # клиент активирован в БД
    row = services.db.get_client_by_tg(42)
    assert row is not None and row.activation_status == "active"
    # пользователю — подтверждение; админу — уведомление об активации
    assert any(s[0] == "answer" for s in msg.sent)
    assert any(r[0] == "send_message" and r[1] == config.ADMIN_ID for r in fake_bot.records)


async def test_try_activate_invalid_code(services, fake_bot):
    msg = FakeMessage(text="/start", chat_id=43, user_id=43, bot=fake_bot)
    await client_h._try_activate(msg, services, "Cnonexistent")
    assert services.db.get_client_by_tg(43) is None
    from awgbot.bot import texts
    assert any(s[1] == texts.ACTIVATION_INVALID for s in msg.sent)


async def test_try_activate_already_has_access(services, make_active_client, fake_bot):
    make_active_client(tg_id=44)
    other = services.create_client("B", 1, "year")
    msg = FakeMessage(text="/start", chat_id=44, user_id=44, bot=fake_bot)
    await client_h._try_activate(msg, services, other.invite_code)
    from awgbot.bot import texts
    assert any(s[1] == texts.ACTIVATION_ALREADY for s in msg.sent)


# ── старт активного клиента → главное меню (send_menu) ───────────────────────
async def test_start_client_shows_menu_and_tracks_nav(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=50)
    msg = FakeMessage(text="/start", chat_id=50, user_id=50, bot=fake_bot)
    await client_h.start_client(msg, client, services, FakeState())
    # меню показано новым сообщением и стало активным нав-сообщением в БД
    assert any(s[0] == "answer" for s in msg.sent)
    assert services.db.get_nav_message_id(50) is not None


# ── меню «Устройства» (edit текущего сообщения) ──────────────────────────────
async def test_menu_devices_lists_devices(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=51)
    services.add_device(client.id, "Телефон")
    nav = FakeMessage(chat_id=51, user_id=51, bot=fake_bot)
    cb = FakeCallback(data="menu", message=nav, user_id=51, bot=fake_bot)
    await client_h.menu_devices(cb, client, services)
    # сообщение отредактировано заголовком «Твои устройства»; callback подтверждён
    assert any(s[0] == "edit_text" and "устройства" in s[1].lower() for s in nav.sent)
    assert cb.answers                                    # cb.answer() вызван


async def test_menu_gen_link_without_devices_alerts(services, make_active_client, fake_bot):
    client = make_active_client(tg_id=52)
    nav = FakeMessage(chat_id=52, user_id=52, bot=fake_bot)
    cb = FakeCallback(data="gen_link", message=nav, user_id=52, bot=fake_bot)
    await client_h.menu_gen_link(cb, client, services)
    # нет устройств → алерт, редактирования нет
    assert cb.answers and cb.answers[0][1] is True       # show_alert=True
    assert not any(s[0] == "edit_text" for s in nav.sent)


