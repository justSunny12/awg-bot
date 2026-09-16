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


async def test_guest_delete_notifies_owner_and_empty_guest_keeps_profile(services, fake_bot,
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
    # последнее — профиль остаётся, главный экран объясняет, что дальше
    cb, nav = _cb(fake_bot, 98106)
    await fh.friend_delete_confirm(cb, DelDeviceCB(device_id=b.device_id, stage="confirm"),
                                   guest, services)
    assert services.db.get_client_by_tg(98106) is not None
    answers = [s for s in nav.sent if s[0] == "answer"]
    assert texts.GUEST_NO_DEVICES_LEFT in answers[-1][1] and "Статус подписки" not in answers[-1][1]
    labels = [b.text for row in answers[-1][2].inline_keyboard for b in row]
    assert labels == ["📱 Мои устройства", "❓ Помощь с настройкой"]


async def test_guest_help_platform(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8107)
    _, guest = _lend(services, owner, 98107)
    cb, nav = _cb(fake_bot, 98107)
    await fh.friend_help(cb)
    cb, nav = _cb(fake_bot, 98107)
    await fh.friend_help_platform(cb, HelpCB(platform="android"))
    assert any(s[0] == "edit_text" for s in nav.sent)


# ── РФ-доступ у гостя (docs/guest-role.md) ─────────────────────────────────

async def test_guest_main_shows_rf_line_and_button_only_with_owner_permission(
        services, fake_bot, make_active_client, monkeypatch):
    from awgbot.core import config
    from awgbot.bot.handlers import routing as rh
    from awgbot.bot.callbacks import RoutingCB
    from tests.conftest import FakeState
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    owner = make_active_client(tg_id=8110, name="Вася", device_limit=3)
    dc, guest = _lend(services, owner, 98110, "Тел")
    text, markup = await fh.guest_main_payload(services, guest)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "РФ-доступ" not in text and not any("РФ" in l for l in labels)

    services.set_routing_allowed(owner.id, True)
    text, markup = await fh.guest_main_payload(services, guest)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "🇷🇺 РФ-доступ:" in text
    assert labels[1] == "🟢 Доступ к РФ-сервисам", labels

    # раздел открывается гостю, «Назад» — на его главный экран
    cb, nav = _cb(fake_bot, 98110)
    await rh.routing_panel(cb, RoutingCB(action="panel", ref=guest.id), guest, services, FakeState())
    text, labels = last_screen(nav)
    assert "РФ-доступ" in text and any("Устройства: 1 из 1" in l for l in labels)
    back = nav.sent[-1][2].inline_keyboard[-1][0].callback_data
    assert back == "fr:refresh:0"


async def test_owner_devices_screen_lists_lent_out_without_toggle(
        services, fake_bot, make_active_client, monkeypatch):
    from awgbot.core import config
    from awgbot.bot.handlers import routing as rh
    from awgbot.bot.callbacks import RoutingCB
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    owner = make_active_client(tg_id=8111, name="Вася", device_limit=3)
    services.set_routing_allowed(owner.id, True)
    services.add_device(owner.id, "Своё")
    lent, _ = _lend(services, owner, 98111, "Ноутбук")
    owner = services.db.get_client(owner.id)
    cb, nav = _cb(fake_bot, 8111)
    await rh.routing_devices_screen(cb, RoutingCB(action="devs", ref=owner.id), owner, services)
    text, labels = last_screen(nav)
    assert "Включено на <b>1</b> из <b>1</b>" in text
    assert texts.ROUTING_LENT_OUT_NOTE in text
    assert labels[-2] == "👤 Ноутбук — Артём"
    rows = nav.sent[-1][2].inline_keyboard
    assert rows[-2][0].callback_data == f"rt:lent:{lent.device_id}:-1"
    cb, _ = _cb(fake_bot, 8111)
    await rh.routing_lent_row(cb)
    assert cb.answers[-1][1] is True


async def test_holder_toggles_held_device_owner_cannot(services, fake_bot, make_active_client,
                                                        monkeypatch):
    """Переключатель — у держателя; владелец своё переданное не трогает даже
    со старой кнопки, админ из чужой панели — тоже."""
    from awgbot.core import config
    from awgbot.bot.handlers import routing as rh
    from awgbot.bot.callbacks import RoutingCB
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    owner = make_active_client(tg_id=8120, device_limit=3)
    services.set_routing_allowed(owner.id, True)
    dc, guest = _lend(services, owner, 98120, "Тел")
    assert services.db.get_device(dc.device_id).routing_on == 1
    cb, _ = _cb(fake_bot, 98120)
    await rh.routing_device_toggle(cb, RoutingCB(action="dev", ref=dc.device_id), guest, services)
    assert cb.answers[-1][0] == "выключено"
    assert services.db.get_device(dc.device_id).routing_on == 0
    owner = services.db.get_client(owner.id)
    cb, _ = _cb(fake_bot, 8120)
    await rh.routing_device_toggle(cb, RoutingCB(action="dev", ref=dc.device_id), owner, services)
    assert cb.answers[-1][1] is True and services.db.get_device(dc.device_id).routing_on == 0
    cb, _ = _cb(fake_bot, config.ADMIN_ID)
    await rh.routing_device_toggle(cb, RoutingCB(action="dev", ref=dc.device_id), None, services)
    assert cb.answers[-1][1] is True and services.db.get_device(dc.device_id).routing_on == 0
