"""E2E: роутер гостя (роль invited, docs/guest-role.md) — главный экран,
«Мои устройства», карточка, выдача, блокировка с подтверждением, удаление с
уведомлением владельца, защита от чужого device_id."""
import pytest

from awgbot.bot import texts
from awgbot.bot.handlers import friend as fh
from awgbot.bot.callbacks import BlockCB, DelDeviceCB, FriendCB, HelpCB
from awgbot.core.blocks import DeviceBlock
from tests.conftest import FakeCallback, FakeMessage, last_screen

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _lend(services, owner, friend_tg, name="d", tg_name="Артём"):
    dc = services.add_device(owner.id, name)
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=friend_tg,
                                   tg_name=tg_name)
    assert res.ok, res.reason
    return dc, res.holder


async def test_guest_main_screen(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8100, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 98100, "Ноут")
    _, guest = _lend(services, owner, 98100, "Тел")
    msg = FakeMessage(text="/start", chat_id=98100, user_id=98100, bot=fake_bot)
    await fh.friend_start(msg, guest, services)
    _, text, markup = [s for s in msg.sent if s[0] == "answer"][-1]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert text.startswith("Привет, Артём! 👋")
    assert "VPN-сервер" in text
    assert 'Статус подписки: 🟢 активна (владелец: <a href="tg://user?id=8100">Вася</a>)' in text
    assert "У тебя 2 устройства" in text
    assert labels[0] == "📱 Мои устройства" and "❓ Помощь с настройкой" in labels
    assert {"🔗 Ссылка", "🔳 QR-код", "📄 Файл"} <= set(labels)


async def test_guest_devices_and_card(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8101, name="Вася", traffic_limit=100 * 1024 ** 3)
    dc, guest = _lend(services, owner, 98101, "Телефон")
    cb, nav = _cb(fake_bot, 98101)
    await fh.friend_list(cb, guest, services)
    text, labels = last_screen(nav)
    assert "У тебя 1 устройство" in text and labels[0].endswith("Телефон")
    cb, nav = _cb(fake_bot, 98101)
    await fh.friend_open(cb, FriendCB(action="open", device_id=dc.device_id), guest, services)
    text, labels = last_screen(nav)
    assert "Телефон" in text and "лимит профиля владельца 100.00 ГБ" in text
    assert '👤 Получено от <a href="tg://user?id=8101">Вася</a>' in text
    assert labels == ["🔌 Данные для подключения", "🛑 Заблокировать", "🗑 Удалить", "⬅️ Назад"]


async def test_guest_foreign_device_guarded(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8102)
    other = make_active_client(tg_id=8103)
    dc, _ = _lend(services, owner, 98102, "d")
    _, stranger = _lend(services, other, 98103, "x")
    cb, nav = _cb(fake_bot, 98103)
    await fh.friend_open(cb, FriendCB(action="open", device_id=dc.device_id), stranger, services)
    assert cb.answers and cb.answers[-1][1] is True
    assert not any(s[0] == "edit_text" for s in nav.sent)


async def test_guest_gen_from_main_picks_when_several(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8104, device_limit=3)
    a, guest = _lend(services, owner, 98104, "A")
    _, guest = _lend(services, owner, 98104, "B")
    cb, nav = _cb(fake_bot, 98104)
    await fh.friend_gen(cb, FriendCB(action="gen_link"), guest, services)
    text, labels = last_screen(nav)
    assert "Для какого устройства нужна ссылка?" == text and labels[:2] == ["A", "B"]
    cb, nav = _cb(fake_bot, 98104)
    await fh.friend_gen(cb, FriendCB(action="gen_link", device_id=a.device_id), guest, services)
    assert any(s[0] == "answer" and "Ссылка для подключения" in s[1] for s in nav.sent)


async def test_guest_block_needs_confirmation(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8105)
    dc, guest = _lend(services, owner, 98105, "Тел")
    cb, nav = _cb(fake_bot, 98105)
    await fh.friend_block_ask(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id),
                              guest, services)
    text, labels = last_screen(nav)
    assert text.startswith("Заблокировать «Тел»?") and labels == ["⬅️ Отмена", "🛑 Заблокировать"]
    assert not int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)
    cb, nav = _cb(fake_bot, 98105)
    await fh.friend_block_do(cb, BlockCB(target="dev", action="block", ref=dc.device_id, kind="user"),
                             guest, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)
    _, labels = last_screen(nav)
    assert "✅ Разблокировать" in labels


async def test_guest_delete_notifies_owner_and_closes_empty_guest(services, fake_bot,
                                                                   make_active_client):
    owner = make_active_client(tg_id=8106, name="Вася", device_limit=3)
    a, guest = _lend(services, owner, 98106, "A")
    b, guest = _lend(services, owner, 98106, "B")
    cb, nav = _cb(fake_bot, 98106)
    await fh.friend_delete_ask(cb, DelDeviceCB(device_id=a.device_id, stage="ask"), guest, services)
    text, labels = last_screen(nav)
    assert text == texts.device_delete_by_holder_ask("A") and "переданное другом" in text
    cb, nav = _cb(fake_bot, 98106)
    fake_bot.records.clear()
    await fh.friend_delete_confirm(cb, DelDeviceCB(device_id=a.device_id, stage="confirm"),
                                   guest, services)
    owner_msgs = [r[2] for r in fake_bot.records if r[0] == "send_message" and r[1] == 8106]
    assert owner_msgs == ['Устройство «A», ранее переданное <a href="tg://user?id=98106">Артём</a>, '
                          'удалено по его запросу.\nТеперь у тебя 1 из 3 устройств.']
    edits = [s for s in nav.sent if s[0] == "edit_text"]
    assert edits[-1][1] == "🗑 Устройство «A» удалено." and edits[-1][2] is None
    answers = [s for s in nav.sent if s[0] == "answer"]
    assert "У тебя 1 устройство" in answers[-1][1] and answers[-1][2] is not None
    # последнее — гость закрывается
    cb, nav = _cb(fake_bot, 98106)
    await fh.friend_delete_confirm(cb, DelDeviceCB(device_id=b.device_id, stage="confirm"),
                                   guest, services)
    assert services.db.get_client_by_tg(98106) is None
    assert any(s[0] == "answer" and s[1] == texts.GUEST_NO_DEVICES_LEFT for s in nav.sent)


async def test_guest_help_platform(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8107)
    _, guest = _lend(services, owner, 98107)
    cb, nav = _cb(fake_bot, 98107)
    await fh.friend_help(cb)
    cb, nav = _cb(fake_bot, 98107)
    await fh.friend_help_platform(cb, HelpCB(platform="android"))
    assert any(s[0] == "edit_text" for s in nav.sent)
