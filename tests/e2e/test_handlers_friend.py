"""E2E: роутер гостя (роль invited) — главный экран,
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
    """Главная гостя: имя, VPN, подписка владельца — только статус, ряд выдачи,
    «Устройства», «Помощь». Без лимитов и без трафика строк трафика нет —
    нули не выводим."""
    owner = make_active_client(tg_id=8100, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 98100, "Ноут")
    _, guest = _lend(services, owner, 98100, "Тел")
    msg = FakeMessage(text="/start", chat_id=98100, user_id=98100, bot=fake_bot)
    await fh.friend_start(msg, guest, services)
    _, text, markup = [s for s in msg.sent if s[0] == "answer"][-1]
    rows = [[b.text for b in row] for row in markup.inline_keyboard]
    lines = text.splitlines()
    assert lines[0] == "👋 Артём" and lines[1].endswith(("VPN работает", "VPN не отвечает")), text
    assert lines[2] == '💳 Подписка профиля <a href="tg://user?id=8100">Вася</a>: 🟢 активна', text
    assert len(lines) == 3, "строки трафика при нулях без лимита"
    assert rows == [["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["📱 Устройства"], ["❓ Помощь"]], rows


async def test_guest_main_screen_consumption_against_limits(services, fake_bot, make_active_client):
    """Лимит устройства — свой, иначе профиля владельца; срок подписки
    дарителя гостю не показываем даже когда владельцу уже напоминали."""
    G = 1024 ** 3
    owner = make_active_client(tg_id=8107, name="Вася", device_limit=3, traffic_limit=50 * G)
    services.db.update_client_fields(owner.id, notified_thresholds="10080")
    dc, guest = _lend(services, owner, 98107, "Тел")
    services.db.add_traffic_bulk([(dc.device_id, 20 * G, 4 * G + G // 10)])
    text, _ = await fh.guest_main_payload(services, guest)
    assert text.splitlines()[-1] == "📊 Тел: 24.1 из 50 ГБ", text
    assert ": 🟢 активна" in text and "истекает" not in text
    services.set_device_traffic_limit(dc.device_id, 30 * G)
    text, _ = await fh.guest_main_payload(services, guest)
    assert text.splitlines()[-1] == "📊 Тел: 24.1 из 30 ГБ", text


async def test_guest_devices_and_card(services, fake_bot, make_active_client):
    """Список и карточка гостя: от кого устройство, чей лимит, ряд выдачи в
    карточке — «Как подключить?» отдельным экраном больше нет."""
    owner = make_active_client(tg_id=8101, name="Вася", traffic_limit=100 * 1024 ** 3)
    dc, guest = _lend(services, owner, 98101, "Телефон")
    cb, nav = _cb(fake_bot, 98101)
    await fh.friend_list(cb, guest, services)
    text, labels = last_screen(nav)
    assert text == '📱 Устройства · 1 · от профиля <a href="tg://user?id=8101">Вася</a>', text
    assert labels == ["⚪ Телефон", "⬅️ Назад"], labels
    cb, nav = _cb(fake_bot, 98101)
    await fh.friend_open(cb, FriendCB(action="open", device_id=dc.device_id), guest, services)
    text, labels = last_screen(nav)
    head, usage = text.splitlines()[:2]
    assert head.startswith("⚪ Телефон · ") and head.endswith(
        '· от профиля <a href="tg://user?id=8101">Вася</a>'), head
    assert usage == "Не подключалось · 0 из 100 ГБ (лимит профиля Вася)", usage
    assert labels == ["🔗 Ссылка", "🔳 QR", "📄 Файл", "🛑 Блок", "🗑 Удалить", "⬅️ Назад"], labels


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
    """С главной: несколько устройств — выбор; по устройству — ссылка и
    пояснение одним сообщением с «⬅️ В меню» на главную гостя."""
    owner = make_active_client(tg_id=8104, device_limit=3)
    a, guest = _lend(services, owner, 98104, "A")
    _, guest = _lend(services, owner, 98104, "B")
    cb, nav = _cb(fake_bot, 98104)
    await fh.friend_gen(cb, FriendCB(action="gen_link"), guest, services)
    text, labels = last_screen(nav)
    assert text == "🔗 Ссылка — для какого устройства?" and labels == ["⚪ A", "⚪ B", "⬅️ Назад"], labels
    cb, nav = _cb(fake_bot, 98104)
    await fh.friend_gen(cb, FriendCB(action="gen_link", device_id=a.device_id), guest, services)
    sent = [s for s in nav.sent if s[0] == "answer"]
    assert len(sent) == 1, "ссылка и пояснение — одним сообщением"
    body, markup = sent[0][1], sent[0][2]
    assert body.startswith("<code>vpn://") and body.endswith(
        "\n\n☝️ Ссылка для A — нажми на неё, чтобы скопировать, и вставь в AmneziaVPN"), body
    assert [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] == [
        ("⬅️ В меню", FriendCB(action="refresh").pack())]
    assert services.db.get_nav_message_id(98104) is not None


async def test_guest_single_device_is_issued_without_a_pick(services, fake_bot, make_active_client):
    """Одно устройство — выдача с главной сразу, экрана выбора нет."""
    owner = make_active_client(tg_id=8108)
    _, guest = _lend(services, owner, 98108, "Тел")
    cb, nav = _cb(fake_bot, 98108)
    await fh.friend_gen(cb, FriendCB(action="gen_qr"), guest, services)
    assert not any(s[0] == "edit_text" for s in nav.sent), "выбор из одного устройства"
    assert [s[1] for s in nav.sent if s[0] == "animation"] == [
        "🔳 Для Тел — в AmneziaVPN «＋» → «Создать из QR-кода», наведи камеру"]


async def test_guest_gen_failure_shows_main_screen_not_a_finisher(services, fake_bot,
                                                                 make_active_client, monkeypatch):
    """Сервер не выдал конфиг: пояснение «☝️ Ссылка для …» под отказом врало
    бы, а меню под кнопкой уже снято — следом главный экран."""
    from awgbot.domain.services import ServiceError
    owner = make_active_client(tg_id=8106)
    dc, guest = _lend(services, owner, 98106, "Тел")

    def boom(device_id, **kw):
        raise ServiceError("сервер не отвечает")
    monkeypatch.setattr(services, "generate_config", boom)
    cb, nav = _cb(fake_bot, 98106)
    await fh.friend_gen(cb, FriendCB(action="gen_link", device_id=dc.device_id), guest, services)
    answers = [s[1] for s in nav.sent if s[0] == "answer"]
    assert any("Не удалось выдать конфиг" in a for a in answers)
    assert not any("☝️" in a for a in answers), "пояснение под отказом"
    assert answers[-1].startswith("👋 "), "главный экран не пришёл"


async def test_guest_block_needs_confirmation(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=8105)
    dc, guest = _lend(services, owner, 98105, "Тел")
    cb, nav = _cb(fake_bot, 98105)
    await fh.friend_block_ask(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id),
                              guest, services)
    text, labels = last_screen(nav)
    assert text == texts.block_device_ask("Тел") and labels == ["⬅️ Отмена", "🛑 Заблокировать"]
    assert not int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)
    cb, nav = _cb(fake_bot, 98105)
    await fh.friend_block_do(cb, BlockCB(target="dev", action="block", ref=dc.device_id, kind="user"),
                             guest, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)
    text, labels = last_screen(nav)
    assert "✅ Разблок" in labels and text.startswith("⛔ Тел"), (text, labels)


async def test_guest_delete_notifies_owner_and_empty_guest_keeps_profile(services, fake_bot,
                                                                          make_active_client):
    owner = make_active_client(tg_id=8106, name="Вася", device_limit=3)
    a, guest = _lend(services, owner, 98106, "A")
    b, guest = _lend(services, owner, 98106, "B")
    cb, nav = _cb(fake_bot, 98106)
    await fh.friend_delete_ask(cb, DelDeviceCB(device_id=a.device_id, stage="ask"), guest, services)
    text, labels = last_screen(nav)
    assert text == "🗑 Удалить A?\nНовое устройство можно будет создать только по коду от друга"
    assert labels == ["⬅️ Отмена", "🗑 Удалить"]
    cb, nav = _cb(fake_bot, 98106)
    fake_bot.records.clear()
    await fh.friend_delete_confirm(cb, DelDeviceCB(device_id=a.device_id, stage="confirm"),
                                   guest, services)
    owner_msgs = [r[2] for r in fake_bot.records if r[0] == "send_message" and r[1] == 8106]
    assert owner_msgs == ['Устройство «A», ранее переданное <a href="tg://user?id=98106">Артём</a>, '
                          'удалено по его запросу.\nТеперь у тебя 1 из 3 устройств.']
    edits = [s for s in nav.sent if s[0] == "edit_text"]
    assert edits[-1][1] == "🗑 A удалено" and edits[-1][2] is None
    answers = [s for s in nav.sent if s[0] == "answer"]
    assert answers[-1][1].startswith("👋 Артём\n") and answers[-1][2] is not None
    # последнее — профиль остаётся, главный экран объясняет, что дальше
    cb, nav = _cb(fake_bot, 98106)
    await fh.friend_delete_confirm(cb, DelDeviceCB(device_id=b.device_id, stage="confirm"),
                                   guest, services)
    assert services.db.get_client_by_tg(98106) is not None
    answers = [s for s in nav.sent if s[0] == "answer"]
    assert answers[-1][1] == "👋 Артём · устройств нет — попроси у друга новый код"
    labels = [b.text for row in answers[-1][2].inline_keyboard for b in row]
    assert labels == ["❓ Помощь"], "без устройств — одна «Помощь», а не пустой экран"


async def test_guest_help_uses_the_same_guides_with_guest_exit(services, fake_bot, make_active_client):
    """Помощь гостя — те же пошаговые гайды со скриншотами, что у клиента;
    выход с экрана платформ — «✅ Всё умею сам», а «⬅️ В меню» в шагах — на
    главную гостя, а не в клиентское меню, которого у него нет."""
    from awgbot.bot.handlers import guide as gh
    owner = make_active_client(tg_id=8107)
    _, guest = _lend(services, owner, 98107)
    cb, nav = _cb(fake_bot, 98107)
    await fh.friend_help(cb)
    text, labels = last_screen(nav)
    assert text == "❓ Помощь — какое устройство?"
    assert labels == ["🍎 iPhone / iPad", "🤖 Android", "🪟 Windows", "🍏 Mac", "✅ Всё умею сам"], labels
    back = nav.sent[-1][2].inline_keyboard[-1][0].callback_data
    assert back == FriendCB(action="refresh").pack()
    cb, nav = _cb(fake_bot, 98107)
    await gh.help_launch(cb, HelpCB(platform="android"), services, guest)
    shown = [s for s in nav.sent if s[0] in ("edit_text", "photo", "answer")]
    assert shown, "гайд гостю не показан"
    menu = [b for row in shown[-1][2].inline_keyboard for b in row if b.text == "⬅️ В меню"]
    assert menu and menu[0].callback_data == FriendCB(action="refresh").pack()


# ── РФ-доступ у гостя ─────────────────────────────────

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
    rows = [[b.text for b in row] for row in markup.inline_keyboard]
    assert "🇷🇺 РФ-доступ" in text.splitlines()[1], text
    assert ["📱 Устройства", "🇷🇺 РФ-доступ"] in rows, rows

    # раздел открывается гостю, «Назад» — на его главный экран
    cb, nav = _cb(fake_bot, 98110)
    await rh.routing_panel(cb, RoutingCB(action="panel", ref=guest.id), guest, services, FakeState())
    text, labels = last_screen(nav)
    assert text.startswith("🇷🇺 РФ-доступ: вкл на всех") and labels[0] == "✅ Тел · от профиля Вася", (text, labels)
    back = nav.sent[-1][2].inline_keyboard[-1][0].callback_data
    assert back == "fr:refresh:0"


async def test_owner_devices_screen_lists_lent_out_without_toggle(
        services, fake_bot, make_active_client, monkeypatch):
    """Переданное — строкой в тексте раздела, без кнопки: включает держатель."""
    from awgbot.core import config
    from awgbot.bot.handlers import routing as rh
    from awgbot.bot.callbacks import RoutingCB
    from tests.conftest import FakeState
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    owner = make_active_client(tg_id=8111, name="Вася", device_limit=3)
    services.set_routing_allowed(owner.id, True)
    services.add_device(owner.id, "Своё")
    _lend(services, owner, 98111, "Ноутбук")
    owner = services.db.get_client(owner.id)
    cb, nav = _cb(fake_bot, 8111)
    await rh.routing_panel(cb, RoutingCB(action="panel", ref=owner.id), owner, services, FakeState())
    text, labels = last_screen(nav)
    assert text.startswith("🇷🇺 РФ-доступ: вкл на всех"), text
    assert 'Ноутбук — у профиля <a href="tg://user?id=98111">Артём</a>, включает он сам' in text
    assert not any("Ноутбук" in l for l in labels), labels
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
