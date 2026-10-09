"""E2E: переданные устройства с точки зрения КЛИЕНТА:
владелец видит переданное с пометкой и может только переименовать/удалить;
клиент-держатель видит чужое после своих, управляет им как держатель;
блокировка — с подтверждением.

Карточки, списки и подтверждения этих веток — в эталоне
tests/screens/client.txt; здесь — БД, уведомления второй стороне и
состояния, которых в эталоне нет (лимит профиля, РФ-байты).
"""
import pytest

from awgbot.bot.handlers import client as ch
from awgbot.bot.callbacks import BlockCB, DelDeviceCB, DeviceCB
from awgbot.core.blocks import DeviceBlock
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


async def test_owner_card_of_lent_device_shows_profile_traffic_limit(services, fake_bot,
                                                                     make_active_client):
    """У профиля с лимитом трафика карточка переданного показывает расход
    против лимита профиля: держатель тратит трафик владельца, и владелец
    должен это видеть."""
    owner = make_active_client(tg_id=7100, name="Вася", device_limit=3, traffic_limit=100 * 1024 ** 3)
    dc = services.add_device(owner.id, "Ноут")
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=97100,
                                   tg_name="Артём")
    assert res.ok
    cb, nav = _cb(fake_bot, 7100)
    await ch.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), owner, services, FakeState())
    text, _ = last_screen(nav)
    assert text.splitlines()[1] == "Не подключался · 📊 0 из 100 ГБ (лимит твоего профиля)", text


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


async def test_holder_and_guest_cards_do_not_show_rf(services, fake_bot, make_active_client):
    """Переданное устройство с РФ-байтами владельца: ни клиент-держатель, ни
    гость по ссылке строки РФ в карточке не видят — это сведения для админа."""
    from awgbot.bot.callbacks import FriendCB
    from awgbot.bot.handlers import friend as fh
    owner = make_active_client(tg_id=7200, name="Вася", device_limit=3)
    services.db.update_client_fields(owner.id, routing_allowed=1)
    holder = make_active_client(tg_id=7201, name="Петя", device_limit=2)
    to_holder = services.add_device(owner.id, "Ноут")
    to_guest = services.add_device(owner.id, "Планшет")
    assert services.activate_friend(services.make_device_friendly(to_holder.device_id), tg_id=7201).ok
    guest = services.activate_friend(services.make_device_friendly(to_guest.device_id), tg_id=97201)
    assert guest.ok, guest.reason

    async def cards():
        cb, nav = _cb(fake_bot, 7201)
        await ch.device_open(cb, DeviceCB(action="open", device_id=to_holder.device_id),
                             services.db.get_client(holder.id), services, FakeState())
        held = last_screen(nav)
        cb, nav = _cb(fake_bot, 97201)
        await fh.friend_open(cb, FriendCB(action="open", device_id=to_guest.device_id),
                             guest.holder, services)
        return held, last_screen(nav)

    before = await cards()
    services.db.rf_add_bulk([(to_holder.device_id, 1024 ** 3, 1024 ** 3),
                             (to_guest.device_id, 1024 ** 3, 1024 ** 3)])
    after = await cards()
    assert after == before, "карточка держателя или гостя изменилась от РФ-данных"
    assert all("🇷🇺" not in text for text, _ in after), after
