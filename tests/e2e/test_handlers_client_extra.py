"""E2E: добор веток роутера клиента — выдача qr/file из меню, приостановка
(resume-ask/cancel), передача другу, добавление устройства другу, активация по
команде /code, холодный старт.
"""
import types

import pytest

from awgbot.bot.handlers import client as ch
from awgbot.bot.callbacks import DeviceCB, PauseCB
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _fresh(services, client):
    return services.db.get_client(client.id)


async def test_menu_gen_qr_and_file(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5100)
    services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5100)
    await ch.menu_gen_qr(cb, cl, services)
    assert any(s[0] == "edit_text" for s in nav.sent)       # пикер устройства
    cb2, nav2 = _cb(fake_bot, 5100)
    await ch.menu_gen_file(cb2, cl, services)
    assert any(s[0] == "edit_text" for s in nav2.sent)


async def test_device_gen_file_sends_conf(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5101)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5101)
    await ch.device_gen_file(cb, DeviceCB(action="gen_file", device_id=dc.device_id), cl, services)
    assert any(s[0] == "document" for s in nav.sent)


async def test_device_transfer_ask(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5102)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5102)
    await ch.device_transfer_ask(cb, DeviceCB(action="transfer_ask", device_id=dc.device_id), cl, services)
    assert any(s[0] == "edit_text" for s in nav.sent)


async def test_device_add_friend_starts_fsm(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5103, device_limit=3)
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5103)
    await ch.device_add_friend(cb, cl, services, st)
    assert (await st.get_data()).get("for_friend") is True


async def test_pause_resume_ask_and_cancel(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5104, period_kind="year")
    services.enter_pause(client.id)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5104)
    await ch.pause_resume_ask(cb, PauseCB(action="resume_ask", ref=client.id), cl, services)
    assert any(s[0] == "edit_text" for s in nav.sent)       # предпросчёт списания
    cb2, nav2 = _cb(fake_bot, 5104)
    await ch.pause_cancel(cb2, PauseCB(action="cancel", ref=client.id), cl, services)
    assert cb2.answers


async def test_code_activation_and_cold_start(services, fake_bot, make_active_client):
    # /code с кодом друга
    owner = make_active_client(tg_id=5105)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    st = FakeState()
    m = FakeMessage(text=f"/code {code}", chat_id=95105, user_id=95105, bot=fake_bot)
    cmd = types.SimpleNamespace(args=code)
    await ch.code_activation(m, cmd, services, st)
    assert services.db.get_device(dc.device_id).friend_tg_id == 95105
    # холодный старт (без кода) — приветствие-заглушка
    m2 = FakeMessage(text="/start", chat_id=95106, user_id=95106, bot=fake_bot)
    st2 = FakeState()
    await ch.start_cold(m2, st2)
    assert any(s[0] == "answer" for s in m2.sent)


async def test_code_activation_empty_arg(services, fake_bot):
    st = FakeState()
    m = FakeMessage(text="/code", chat_id=95107, user_id=95107, bot=fake_bot)
    cmd = types.SimpleNamespace(args=None)
    await ch.code_activation(m, cmd, services, st)
    assert any(s[0] == "answer" for s in m.sent)            # просьба прислать код
