"""E2E: переданные устройства с точки зрения КЛИЕНТА:
владелец видит переданное с пометкой и может только переименовать/удалить;
клиент-держатель видит чужое после своих, управляет им как держатель;
блокировка — с подтверждением.

Карточки, списки и подтверждения этих веток (в том числе с лимитом профиля
и с РФ-байтами владельца) — в эталонах tests/screens/{client,guest}.txt;
здесь — БД и уведомления второй стороне.
"""
import pytest

from awgbot.bot.handlers import client as ch
from awgbot.bot.callbacks import BlockCB, DelDeviceCB
from awgbot.core.blocks import DeviceBlock
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


async def test_owner_delete_of_lent_device_notifies_holder(services, fake_bot, make_active_client):
    """Владелец удалил переданное — держатель узнаёт, что доступ пропал, а
    его гостевой профиль остаётся (иначе он потеряет и остальные устройства)."""
    owner = make_active_client(tg_id=7101, name="Вася", device_limit=3)
    dc = services.add_device(owner.id, "Ноут")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=97101, tg_name="Артём")
    cb, nav = _cb(fake_bot, 7101)
    fake_bot.records.clear()
    await ch.device_delete_confirm(cb, DelDeviceCB(device_id=dc.device_id, stage="confirm"),
                                   owner, services)
    holder_msgs = [r[2] for r in fake_bot.records if r[0] == "send_message" and r[1] == 97101]
    assert holder_msgs == ['Устройство «Ноут», которым ты управлял, удалено владельцем '
                           '(<a href="tg://user?id=7101">Вася</a>) — доступ по нему больше не работает.']
    assert services.db.get_client_by_tg(97101) is not None, "профиль гостя не терминируется"


async def test_client_holder_delete_of_foreign_device_notifies_owner(services, fake_bot,
                                                                     make_active_client):
    """Клиент-держатель удалил чужое: владельцу — уведомление с его новым
    счётом устройств, профиль держателя цел."""
    owner = make_active_client(tg_id=7102, name="Вася", device_limit=3)
    holder = make_active_client(tg_id=7103, name="Петя", device_limit=2)
    services.add_device(holder.id, "Своё")
    dc = services.add_device(owner.id, "Чужое")
    assert services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=7103).ok
    cb, nav = _cb(fake_bot, 7103)
    fake_bot.records.clear()
    await ch.device_delete_confirm(cb, DelDeviceCB(device_id=dc.device_id, stage="confirm"),
                                   holder, services)
    owner_msgs = [r[2] for r in fake_bot.records if r[0] == "send_message" and r[1] == 7102]
    assert owner_msgs and "удалено по его запросу" in owner_msgs[0] and "0 из 3" in owner_msgs[0]
    assert services.db.get_client(holder.id) is not None
    assert services.db.get_device(dc.device_id) is None, "чужое устройство осталось в БД"


async def test_client_block_own_device_needs_confirmation(services, fake_bot, make_active_client):
    """Вопрос о блокировке ничего не блокирует; блокирует только «🛑
    Заблокировать» — иначе случайное нажатие отрезает человека от VPN."""
    cl = make_active_client(tg_id=7104)
    dc = services.add_device(cl.id, "Тел")
    cb, nav = _cb(fake_bot, 7104)
    await ch.client_block_ask(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id), cl, services)
    assert not int(services.db.get_device(dc.device_id).block_reason)
    cb, nav = _cb(fake_bot, 7104)
    await ch.client_block_device(cb, BlockCB(target="dev", action="block", ref=dc.device_id, kind="user"),
                                 cl, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)


async def test_created_for_friend_finisher(services, fake_bot, make_active_client):
    """Ввод «✏️ Другое» при создании другу: «0» — по лимиту профиля, без
    своего лимита; устройство сразу ждёт друга."""
    cl = make_active_client(tg_id=7105, device_limit=3, traffic_limit=50 * 1024 ** 3)
    st = FakeState(); await st.update_data(dev_name="Другу", for_friend=True)
    typed = FakeMessage(text="0", chat_id=7105, user_id=7105, bot=fake_bot)
    await ch.device_add_traffic(typed, cl, services, st)
    dev = services.db.list_devices(cl.id)[0]
    assert dev.name == "Другу" and dev.traffic_limit == 0 and dev.friend_status == "pending"
