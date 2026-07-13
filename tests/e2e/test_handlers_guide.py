"""E2E: визард-гайды (handlers/guide.py) — запуск, навигация по шагам,
интерактивный шаг подключения, добавление устройства внутри гайда.
"""
import pytest

from awgbot.bot.handlers import guide as gh
from awgbot.bot.callbacks import DeviceCB, GuideCB, HelpCB
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


async def test_help_launch_and_step_nav(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6200)
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6200)
    await gh.help_launch(cb, HelpCB(platform="apple"), services, cl)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _cb(fake_bot, 6200)
    await gh.guide_step(cb2, GuideCB(guide="apple", step=1), services, cl)
    # шаг 1 apple теперь со скриншотом → фото (тип сменился с текста, пересоздание)
    assert any(s[0] == "photo" for s in nav2.sent)


async def test_guide_connect_step0_lists_devices(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6201)
    services.add_device(client.id, "d")
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6201)
    await gh.guide_step(cb, GuideCB(guide="connect", step=0), services, cl)
    assert any(s[0] == "edit_text" for s in nav.sent)       # интерактивный шаг 0 с устройствами


async def test_guide_pick_device_delivers_config(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6202)
    dc = services.add_device(client.id, "d")
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6202)
    await gh.guide_pick_device(cb, DeviceCB(action="gen_guide", device_id=dc.device_id), services, cl)
    # теперь не авто-выдача link+file, а шаг настройки с кнопками выбора способа
    answers = [s for s in nav.sent if s[0] == "answer"]
    assert answers and answers[-1][2] is not None            # есть клавиатура выбора способа
    assert not any(s[0] == "document" for s in nav.sent)     # файл сам не уходит


async def test_guide_method_delivers_and_advances(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6212)
    dc = services.add_device(client.id, "d")
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6212)
    # выбор способа «файл» на шаге 1 → выдаётся файл и показывается шаг 2
    await gh.guide_connect_deliver(
        cb, GuideCB(guide="connect", step=1, dev=dc.device_id, kind="file"), services, cl)
    assert any(s[0] == "document" for s in nav.sent)         # артефакт выдан
    assert any(s[0] == "answer" and "Подключаемся" in (s[1] or "") for s in nav.sent)


async def test_guide_method_back_reshows_choice(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6213)
    dc = services.add_device(client.id, "d")
    cl = services.db.get_client(client.id)
    cb, nav = _cb(fake_bot, 6213)
    # «Назад» на шаге 2 → снова выбор способа для того же устройства
    await gh.guide_connect_methods(
        cb, GuideCB(guide="connect", step=1, dev=dc.device_id), services, cl)
    assert any(s[0] == "edit_text" and s[2] is not None for s in nav.sent)


async def test_guide_add_device_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6203, device_limit=3)
    cl = services.db.get_client(client.id)
    st = FakeState()
    cb, nav = _cb(fake_bot, 6203)
    await gh.guide_add_device(cb, GuideCB(guide="connect", step=-1), services, cl, st)
    m_name = FakeMessage(text="Дев", chat_id=6203, user_id=6203, bot=fake_bot)
    await gh.guide_add_device_name(m_name, services, cl, st)
    m_tr = FakeMessage(text="0", chat_id=6203, user_id=6203, bot=fake_bot)
    await gh.guide_add_device_traffic(m_tr, services, cl, st)
    assert any(d.name == "Дев" for d in services.db.list_devices(client.id))


async def test_guide_add_device_full_limit(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6204, device_limit=1)
    services.add_device(client.id, "occupied")
    cl = services.db.get_client(client.id)
    st = FakeState()
    cb, nav = _cb(fake_bot, 6204)
    await gh.guide_add_device(cb, GuideCB(guide="connect", step=-1), services, cl, st)
    assert cb.answers[-1][1] is True                        # лимит исчерпан → alert


async def test_guide_add_name_empty_rejected(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6205)
    cl = services.db.get_client(client.id)
    st = FakeState()
    await st.update_data(return_guide="connect")
    m = FakeMessage(text="  ", chat_id=6205, user_id=6205, bot=fake_bot)
    await gh.guide_add_device_name(m, services, cl, st)
    assert any(s[0] == "answer" for s in m.sent)
