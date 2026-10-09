"""E2E: добор веток роутера клиента — выдача qr/file из меню, приостановка
(отмена), передача другу, добавление устройства другу, активация по команде
/code.

Тексты и кнопки этих экранов (и выдача QR/файла с главной при одном
устройстве) — в эталоне tests/screens/client.txt; здесь — БД и память
диалога.
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


async def test_device_transfer_ask_changes_nothing(services, fake_bot, make_active_client):
    """Вопрос «Передать другу?» только спрашивает: код друга появляется лишь
    после «👤 Передать»."""
    client = make_active_client(tg_id=5102)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5102)
    await ch.device_transfer_ask(cb, DeviceCB(action="transfer", device_id=dc.device_id), cl, services)
    assert services.db.get_device(dc.device_id).friend_status is None, "вопрос ничего не передаёт"


async def test_device_add_friend_creates_device_only_after_limit(
        services, fake_bot, make_active_client):
    """Другу: «👤 Это для друга» ставит флаг в диалоге; после имени устройства
    ещё нет — оно появляется только с выбранным пресетом лимита, сразу с
    ожиданием друга. Созданное до выбора лимита ушло бы без ограничения."""
    from awgbot.bot.callbacks import PresetCB
    G = 1024 ** 3
    client = make_active_client(tg_id=5103, device_limit=3, traffic_limit=100 * G)
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5103)
    await ch.device_add_start(cb, DeviceCB(action="add"), cl, services, st)
    await ch.device_add_for_whom(cb, DeviceCB(action="add_friend"), cl, services, st)
    assert (await st.get_data()).get("for_friend") is True

    typed = FakeMessage(text="Планшет", chat_id=5103, user_id=5103, bot=fake_bot)
    await ch.device_add_name(typed, cl, services, st)
    assert services.db.list_devices(client.id) == [], "устройство создано до выбора лимита"

    cb2, nav2 = _cb(fake_bot, 5103)
    await ch.device_add_limit_preset(cb2, PresetCB(kind="devlimit", ref=0, val=50), cl, services, st)
    dev = services.db.list_devices(client.id)[0]
    assert dev.name == "Планшет" and dev.traffic_limit == 50 * G and dev.friend_status == "pending"
    assert dev.friend_code, "приглашению нечего нести — кода друга нет"


async def test_device_add_friend_other_limit_refuses_above_profile(services, fake_bot, make_active_client):
    """«✏️ Другое» больше лимита профиля не примет: устройства нет."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=5106, device_limit=3, traffic_limit=100 * 1024 ** 3)
    cl = _fresh(services, client)
    st = FakeState()
    await st.set_state(__import__("awgbot.bot.states", fromlist=["AddDevice"]).AddDevice.traffic)
    await st.update_data(for_friend=True, dev_name="Планшет", ctx_kind="main")
    cb, nav = _cb(fake_bot, 5106)
    await ch.device_add_limit_preset(cb, PresetCB(kind="devlimit", ref=0, val=-1), cl, services, st)
    typed = FakeMessage(text="101", chat_id=5106, user_id=5106, bot=fake_bot)
    await ch.device_add_traffic(typed, cl, services, st)
    assert services.db.list_devices(client.id) == []


async def test_pause_cancel_does_not_pause(services, fake_bot, make_active_client):
    """«⬅️ Отмена» в выборе паузы подписку не приостанавливает."""
    client = make_active_client(tg_id=5104, period_kind="year")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5104)
    await ch.pause_cancel(cb, PauseCB(action="cancel", ref=client.id), cl, services, FakeState())
    assert not _fresh(services, client).is_paused


async def test_code_activation_binds_friend_device(services, fake_bot, make_active_client):
    """/code с кодом друга закрепляет устройство за пришедшим."""
    owner = make_active_client(tg_id=5105)
    dc = services.add_device(owner.id, "d")
    code = services.make_device_friendly(dc.device_id)
    st = FakeState()
    m = FakeMessage(text=f"/code {code}", chat_id=95105, user_id=95105, bot=fake_bot)
    cmd = types.SimpleNamespace(args=code)
    await ch.code_activation(m, cmd, services, st)
    assert services.db.get_device(dc.device_id).friend_tg_id == 95105
