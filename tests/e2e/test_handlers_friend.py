"""E2E: роутер гостя (роль invited) — потребление против лимитов, выдача,
блокировка с подтверждением, удаление с уведомлением владельца, РФ-доступ
у держателя, сброс диалога командой.

Экраны гостя (главная, устройства, карточка, выбор, подтверждения, помощь,
раздел РФ-доступа) — в эталоне tests/screens/guest.txt (gst.*)."""
import pytest

from awgbot.bot.handlers import friend as fh
from awgbot.bot.callbacks import BlockCB, DelDeviceCB, FriendCB
from awgbot.core.blocks import DeviceBlock
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

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


async def test_guest_card_names_whose_traffic_limit_applies(services, fake_bot, make_active_client):
    """Карточка гостя при лимите трафика у владельца: чей это лимит — иначе
    гость не поймёт, почему устройство встало, не израсходовав «своё»."""
    owner = make_active_client(tg_id=8101, name="Вася", traffic_limit=100 * 1024 ** 3)
    dc, guest = _lend(services, owner, 98101, "Телефон")
    cb, nav = _cb(fake_bot, 98101)
    await fh.friend_open(cb, FriendCB(action="open", device_id=dc.device_id), guest, services)
    text, _ = last_screen(nav)
    usage = text.splitlines()[1]
    assert usage == "Не подключался · 📊 0 из 100 ГБ (лимит профиля Вася)", usage


async def test_guest_issue_message_becomes_the_guest_menu(services, fake_bot, make_active_client):
    """Выдача по устройству: сообщение со ссылкой становится меню гостя —
    его «⬅️ В меню» потом правится на месте, а не плодит новое."""
    owner = make_active_client(tg_id=8104, device_limit=3)
    a, guest = _lend(services, owner, 98104, "A")
    _, guest = _lend(services, owner, 98104, "B")
    cb, nav = _cb(fake_bot, 98104)
    await fh.friend_gen(cb, FriendCB(action="gen_link", device_id=a.device_id), guest, services)
    assert services.db.get_nav_message_id(98104) is not None


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
    """Вопрос о блокировке сам ничего не блокирует — только подтверждение."""
    owner = make_active_client(tg_id=8105)
    dc, guest = _lend(services, owner, 98105, "Тел")
    cb, nav = _cb(fake_bot, 98105)
    await fh.friend_block_ask(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id),
                              guest, services)
    assert not int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)
    cb, nav = _cb(fake_bot, 98105)
    await fh.friend_block_do(cb, BlockCB(target="dev", action="block", ref=dc.device_id, kind="user"),
                             guest, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)


async def test_guest_delete_notifies_owner_and_empty_guest_keeps_profile(services, fake_bot,
                                                                          make_active_client):
    """Удаление гостем — владельцу уведомление с новым счётом; последнее
    удалённое не уносит профиль гостя: по новому коду друга он вернётся
    в тот же профиль."""
    owner = make_active_client(tg_id=8106, name="Вася", device_limit=3)
    a, guest = _lend(services, owner, 98106, "A")
    b, guest = _lend(services, owner, 98106, "B")
    cb, nav = _cb(fake_bot, 98106)
    fake_bot.records.clear()
    await fh.friend_delete_confirm(cb, DelDeviceCB(device_id=a.device_id, stage="confirm"),
                                   guest, services)
    owner_msgs = [r[2] for r in fake_bot.records if r[0] == "send_message" and r[1] == 8106]
    assert owner_msgs == ['Устройство «A», ранее переданное <a href="tg://user?id=98106">Артём</a>, '
                          'удалено по его запросу.\nТеперь у тебя 1 из 3 устройств.']
    # последнее — профиль остаётся
    cb, nav = _cb(fake_bot, 98106)
    await fh.friend_delete_confirm(cb, DelDeviceCB(device_id=b.device_id, stage="confirm"),
                                   guest, services)
    assert services.db.get_client_by_tg(98106) is not None


# ── РФ-доступ у гостя ─────────────────────────────────

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
    assert services.db.get_device(dc.device_id).routing_on == 0
    owner = services.db.get_client(owner.id)
    cb, _ = _cb(fake_bot, 8120)
    await rh.routing_device_toggle(cb, RoutingCB(action="dev", ref=dc.device_id), owner, services)
    assert cb.answers[-1][1] is True and services.db.get_device(dc.device_id).routing_on == 0
    cb, _ = _cb(fake_bot, config.ADMIN_ID)
    await rh.routing_device_toggle(cb, RoutingCB(action="dev", ref=dc.device_id), None, services)
    assert cb.answers[-1][1] is True and services.db.get_device(dc.device_id).routing_on == 0


async def test_start_and_code_reset_the_dialog_of_a_guest(services, fake_bot, make_active_client):
    """У гостя /start и /code не сбрасывали FSM: текст после «➕ Сайт» уходил в
    добавление домена вместо ответа на команду."""
    owner = make_active_client(tg_id=8150, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 98150, "Ноут")
    st = FakeState()
    await st.set_state("Routing:domain")
    await st.update_data(ctx_kind="sites", ctx_ref=1)
    msg = FakeMessage(text="/start", chat_id=98150, user_id=98150, bot=fake_bot)
    await fh.friend_start(msg, guest, services, st)
    assert await st.get_state() is None and await st.get_data() == {}
