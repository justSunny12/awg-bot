"""E2E: переданные устройства с точки зрения КЛИЕНТА (концепт «гость»):
владелец видит переданное с пометкой и может только переименовать/удалить;
клиент-держатель видит чужое после своих, управляет им как держатель;
блокировка — с подтверждением."""
import pytest

from awgbot.bot import texts
from awgbot.bot.handlers import client as ch
from awgbot.bot.callbacks import BlockCB, DelDeviceCB, DeviceCB
from awgbot.core.blocks import DeviceBlock
from tests.conftest import FakeCallback, FakeMessage, last_screen

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


async def test_owner_sees_lent_device_with_holder_and_limited_buttons(services, fake_bot,
                                                                       make_active_client):
    owner = make_active_client(tg_id=7100, name="Вася", device_limit=3)
    dc = services.add_device(owner.id, "Ноут")
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=97100,
                                   tg_name="Артём")
    assert res.ok
    cb, nav = _cb(fake_bot, 7100)
    await ch.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), owner, services)
    text, labels = last_screen(nav)
    assert '👤 Передано <a href="tg://user?id=97100">Артём</a> и управляется им' in text
    assert labels == ["✏️ Имя", "📊 Лимит потребления", "🗑 Удалить", "⬅️ Назад"]
    # список: своё переданное — 📲
    cb, nav = _cb(fake_bot, 7100)
    await ch.menu_devices(cb, owner, services)
    _, labels = last_screen(nav)
    assert labels[0].startswith("📲 Ноут")


async def test_owner_delete_of_lent_device_warns_and_notifies_holder(services, fake_bot,
                                                                     make_active_client):
    owner = make_active_client(tg_id=7101, name="Вася", device_limit=3)
    dc = services.add_device(owner.id, "Ноут")
    services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=97101, tg_name="Артём")
    cb, nav = _cb(fake_bot, 7101)
    await ch.device_delete_ask(cb, DelDeviceCB(device_id=dc.device_id, stage="ask"), owner, services)
    text, _ = last_screen(nav)
    assert text.startswith("Удалить «Ноут»? Устройство передано <a href=\"tg://user?id=97101\">Артём</a>:")
    cb, nav = _cb(fake_bot, 7101)
    fake_bot.records.clear()
    await ch.device_delete_confirm(cb, DelDeviceCB(device_id=dc.device_id, stage="confirm"),
                                   owner, services)
    holder_msgs = [r[2] for r in fake_bot.records if r[0] == "send_message" and r[1] == 97101]
    assert holder_msgs == ['Устройство «Ноут», которым ты управлял, удалено владельцем '
                           '(<a href="tg://user?id=7101">Вася</a>) — доступ по нему больше не работает.']
    assert services.db.get_client_by_tg(97101) is not None, "профиль гостя не терминируется"


async def test_client_holder_sees_foreign_device_after_own(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=7102, name="Вася", device_limit=3)
    holder = make_active_client(tg_id=7103, name="Петя", device_limit=2)
    services.add_device(holder.id, "Своё")
    dc = services.add_device(owner.id, "Чужое")
    assert services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=7103).ok
    # главный экран: «+ 1 от [Вася]»
    text, _ = await ch._greeting(services, holder)
    assert 'Устройств добавлено: 1 из 2 (+ 1 от <a href="tg://user?id=7102">Вася</a>).' in text
    cb, nav = _cb(fake_bot, 7103)
    await ch.menu_devices(cb, holder, services)
    _, labels = last_screen(nav)
    assert labels[0].startswith("📱 Своё") and labels[1] == "👤 Чужое — от Вася"
    # карточка держателя: без имени, с блокировкой и удалением
    cb, nav = _cb(fake_bot, 7103)
    await ch.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), holder, services)
    text, labels = last_screen(nav)
    assert '👤 Получено от <a href="tg://user?id=7102">Вася</a>' in text
    assert labels == ["🔌 Данные для подключения", "🛑 Заблокировать", "🗑 Удалить", "⬅️ Назад"]
    # удаление держателем: строгий текст, владельцу — уведомление, список следом
    cb, nav = _cb(fake_bot, 7103)
    await ch.device_delete_ask(cb, DelDeviceCB(device_id=dc.device_id, stage="ask"), holder, services)
    assert last_screen(nav)[0] == texts.device_delete_by_holder_ask("Чужое")
    cb, nav = _cb(fake_bot, 7103)
    fake_bot.records.clear()
    await ch.device_delete_confirm(cb, DelDeviceCB(device_id=dc.device_id, stage="confirm"),
                                   holder, services)
    owner_msgs = [r[2] for r in fake_bot.records if r[0] == "send_message" and r[1] == 7102]
    assert owner_msgs and "удалено по его запросу" in owner_msgs[0] and "0 из 3" in owner_msgs[0]
    assert services.db.get_client(holder.id) is not None
    answers = [s for s in nav.sent if s[0] == "answer"]
    assert "Твои устройства" in answers[-1][1]


async def test_client_block_own_device_needs_confirmation(services, fake_bot, make_active_client):
    cl = make_active_client(tg_id=7104)
    dc = services.add_device(cl.id, "Тел")
    cb, nav = _cb(fake_bot, 7104)
    await ch.client_block_ask(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id), cl, services)
    text, labels = last_screen(nav)
    assert text == texts.block_device_ask("Тел") and labels == ["⬅️ Отмена", "🛑 Заблокировать"]
    assert not int(services.db.get_device(dc.device_id).block_reason)
    cb, nav = _cb(fake_bot, 7104)
    await ch.client_block_device(cb, BlockCB(target="dev", action="block", ref=dc.device_id, kind="user"),
                                 cl, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)
    # переданное своё владелец не блокирует — управляет держатель
    other = services.add_device(cl.id, "Отдано")
    services.activate_friend(services.make_device_friendly(other.device_id), tg_id=97104)
    cb, nav = _cb(fake_bot, 7104)
    await ch.client_block_ask(cb, BlockCB(target="dev", action="menu_block", ref=other.device_id), cl, services)
    assert cb.answers[-1][1] is True


async def test_created_for_friend_finisher(services, fake_bot, make_active_client):
    from tests.conftest import FakeState
    cl = make_active_client(tg_id=7105, device_limit=3, traffic_limit=50 * 1024 ** 3)
    st = FakeState(); await st.update_data(dev_name="Другу", for_friend=True)
    typed = FakeMessage(text="0", chat_id=7105, user_id=7105, bot=fake_bot)
    await ch.device_add_traffic(typed, cl, services, st)
    fin = [s[1] for s in typed.sent if s[0] == "answer"][0]
    assert fin == ("✅ Устройство «Другу» создано для друга.\n"
                   "Потребление устройства не ограничено в рамках твоего лимита профиля.\n"
                   "Количество устройств: 1/3")
