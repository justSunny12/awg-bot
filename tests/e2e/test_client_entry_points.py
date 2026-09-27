"""E2E: входы в экраны клиента и гостя помимо кнопок — кнопка из старого
меню (обработчик устаревших), ссылки /start sub|rf|dev-<id>, коды C…/F… через
тот же /start; и главная клиента по строкам.

Цена ошибки: устаревшая кнопка без обработчика крутит часики и молчит;
ссылка из уведомления, открывающая чужое устройство, — утечка; ссылка,
принятая за код, — «такого кода нет» вместо экрана.
"""
import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.filters import CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from awgbot.bot.handlers import client as ch
from awgbot.bot.handlers import friend as fh
from awgbot.bot.handlers import stale
from awgbot.bot.handlers.common import show_main_menu
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e
G = 1024 ** 3


def _cmd(args):
    return CommandObject(prefix="/", command="start", args=args)


def _lend(services, owner, tg, name, tg_name="Артём"):
    dc = services.add_device(owner.id, name)
    res = services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=tg, tg_name=tg_name)
    assert res.ok, res.reason
    return dc, res.holder


# ── устаревшая кнопка ────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", ["client", "invited"])
async def test_stale_button_answers_with_a_popup_and_opens_main(services, fake_bot, make_active_client, role):
    """Кнопка из меню прежней версии: всплывашка «Кнопка устарела — открыл
    меню», кнопки со старого сообщения сняты, главная роли — новым
    сообщением и живым меню."""
    owner = make_active_client(tg_id=9100, name="Вася")
    if role == "client":
        who, uid = owner, 9100
    else:
        _, who = _lend(services, owner, 99100, "Тел")
        uid = 99100
    router = stale.make_router(show_main_menu)
    old = FakeMessage(chat_id=uid, user_id=uid, bot=fake_bot)
    cb = FakeCallback(data="cs:manage:1", message=old, user_id=uid, bot=fake_bot)
    await router.stale_button(cb, services, role=role, client=who)
    assert cb.answers == [("Кнопка устарела — открыл меню", False)]
    assert ("edit_reply_markup", uid) in fake_bot.records, "у старого сообщения живые кнопки"
    shown = [s for s in old.sent if s[0] == "answer"]
    assert len(shown) == 1 and shown[0][1].startswith("👋 ") and shown[0][2] is not None, old.sent
    assert services.db.get_nav_message_id(uid) not in (None, old.message_id)


class _Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return True

    async def stream_content(self, *a, **k):              # pragma: no cover
        raise AssertionError("скачиваний нет")
        yield b""

    async def close(self):
        pass


async def test_stale_router_catches_any_callback_no_screen_took(services):
    """Роутер устаревших — без фильтров: колбэк, который не разобрал ни один
    экран (другой набор полей, снятое действие), доходит до него, а не
    остаётся со спиннером."""
    shown = []

    async def show_main(message, services_, role, client):
        shown.append((message.chat.id, role))

    dp = Dispatcher(storage=MemoryStorage())
    dp["services"] = services
    dp.include_router(stale.make_router(show_main))
    session = _Session()
    bot = Bot("42:DUMMY", session=session)
    msg = Message(message_id=7, date=datetime.datetime.now(), chat=Chat(id=4242, type="private"), text="меню")
    upd = Update(update_id=1, callback_query=CallbackQuery(
        id="q", chat_instance="ci", **{"from": User(id=4242, is_bot=False, first_name="A")},
        message=msg, data="c:manage_sub:1:x"))
    await dp.feed_update(bot, upd, role="client")
    assert shown == [(4242, "client")], "главная не показана"
    answers = [m for m in session.calls if type(m).__name__ == "AnswerCallbackQuery"]
    assert answers and answers[0].text == stale.STALE_BUTTON


# ── ссылки /start у клиента ──────────────────────────────────────────────────

async def _start(services, fake_bot, cl, payload, *, nav_id=None):
    msg = FakeMessage(text=f"/start {payload}", chat_id=cl.tg_id, user_id=cl.tg_id, bot=fake_bot)
    if nav_id is not None:
        services.db.nav_touch(cl.tg_id, nav_id)
    fake_bot.records.clear()
    await ch.start_client_with_code(msg, _cmd(payload), cl, services, FakeState())
    return msg


def _screen(fake_bot, msg):
    """Что человек увидел: экран на месте живого меню (правка) или новым
    сообщением, если живого меню не было."""
    edits = [r[2] for r in fake_bot.records if r[0] == "edit_message_text"]
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    shown = edits + answers
    assert shown, "экран не показан"
    return shown[-1]


async def test_client_link_sub_opens_subscription_in_place_of_the_live_menu(
        services, fake_bot, make_active_client):
    """«/start sub» из строки главной: экран подписки — на месте живого меню,
    сама команда убрана из чата."""
    cl = make_active_client(tg_id=9110, period_kind="year")
    msg = await _start(services, fake_bot, cl, "sub", nav_id=555)
    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert len(edits) == 1 and edits[0][2].startswith("💳 Подписка: годовая · 🟢 активна"), fake_bot.records
    assert msg.deleted, "команда /start осталась в чате"
    assert not any(s[0] == "answer" for s in msg.sent), "второе меню вместо правки живого"


async def test_client_link_without_live_menu_sends_the_screen(services, fake_bot, make_active_client):
    cl = make_active_client(tg_id=9111, period_kind="year")
    services.db.set_nav_message_id(9111, None)
    msg = await _start(services, fake_bot, cl, "sub")
    shown = [s for s in msg.sent if s[0] == "answer"]
    assert shown and shown[-1][1].startswith("💳 Подписка:") and shown[-1][2] is not None


async def test_client_link_rf_opens_the_section_only_when_granted(services, fake_bot, make_active_client):
    cl = make_active_client(tg_id=9112)
    services.add_device(cl.id, "Тел")
    msg = await _start(services, fake_bot, cl, "rf")
    assert _screen(fake_bot, msg).startswith("👋 "), "раздела нет — главная, а не пустота"
    services.set_routing_allowed(cl.id, True)
    cl = services.db.get_client(cl.id)
    msg = await _start(services, fake_bot, cl, "rf")
    assert _screen(fake_bot, msg).startswith("🇷🇺 РФ-доступ: вкл на всех")


async def test_client_link_dev_opens_own_or_held_card_and_refuses_foreign(
        services, fake_bot, make_active_client):
    """«/start dev-<id>» — своё или удерживаемое: карточка; чужое — главная
    (о чужом устройстве ни слова)."""
    owner = make_active_client(tg_id=9113, name="Вася", device_limit=3)
    cl = make_active_client(tg_id=9114, name="Петя")
    own = services.add_device(cl.id, "Своё")
    held = services.add_device(owner.id, "Держит")
    assert services.activate_friend(services.make_device_friendly(held.device_id), tg_id=9114).ok
    foreign = services.add_device(owner.id, "Чужое")
    cl = services.db.get_client(cl.id)
    for dev_id, head in ((own.device_id, "⚪ Своё · "), (held.device_id, "⚪ Держит · ")):
        msg = await _start(services, fake_bot, cl, f"dev-{dev_id}")
        assert _screen(fake_bot, msg).startswith(head)
    msg = await _start(services, fake_bot, cl, f"dev-{foreign.device_id}")
    shown = _screen(fake_bot, msg)
    assert shown.startswith("👋 Петя\n") and "Чужое" not in shown, shown
    msg = await _start(services, fake_bot, cl, "dev-99999999")
    assert _screen(fake_bot, msg).startswith("👋 ")


async def test_friend_code_through_start_still_activates_for_a_client(
        services, fake_bot, make_active_client):
    """Код друга по «/start F…» у действующего клиента — не ссылка на экран,
    а устройство в держание, как раньше."""
    owner = make_active_client(tg_id=9115, name="Вася", device_limit=3)
    cl = make_active_client(tg_id=9116, name="Петя")
    dc = services.add_device(owner.id, "Планшет")
    code = services.make_device_friendly(dc.device_id)
    await _start(services, fake_bot, cl, code)
    assert services.db.get_device(dc.device_id).holder_client_id == cl.id


# ── ссылки /start у гостя ────────────────────────────────────────────────────

async def _guest_start(services, fake_bot, guest, payload):
    msg = FakeMessage(text=f"/start {payload}", chat_id=guest.tg_id, user_id=guest.tg_id, bot=fake_bot)
    await fh.friend_start_with_code(msg, _cmd(payload), guest, services)
    return [s for s in msg.sent if s[0] == "answer"]


async def test_guest_links_open_held_card_and_refuse_foreign(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=9120, name="Вася", device_limit=3)
    dc, guest = _lend(services, owner, 99120, "Тел")
    other = services.add_device(owner.id, "Не его")
    shown = await _guest_start(services, fake_bot, guest, f"dev-{dc.device_id}")
    assert shown[-1][1].startswith("⚪ Тел · "), shown
    shown = await _guest_start(services, fake_bot, guest, f"dev-{other.device_id}")
    assert shown[-1][1].startswith("👋 Артём\n") and "Не его" not in shown[-1][1], shown
    shown = await _guest_start(services, fake_bot, guest, "sub")
    assert shown[-1][1].startswith("👋 Артём\n"), "у гостя своей подписки нет — главная"


async def test_guest_link_rf_opens_the_section_with_owner_permission(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=9121, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 99121, "Тел")
    services.set_routing_allowed(owner.id, True)
    shown = await _guest_start(services, fake_bot, guest, "rf")
    assert shown[-1][1].startswith("🇷🇺 РФ-доступ"), shown
    labels = [b.text for r in shown[-1][2].inline_keyboard for b in r]
    assert labels[0] == "✅ Тел · от профиля Вася", labels


async def test_guest_second_code_through_start_still_activates(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=9122, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 99122, "Тел")
    d2 = services.add_device(owner.id, "Ноут")
    await _guest_start(services, fake_bot, guest, services.make_device_friendly(d2.device_id))
    assert services.db.get_device(d2.device_id).holder_client_id == guest.id


# ── главная клиента ──────────────────────────────────────────────────────────

async def test_client_main_four_lines_with_devices(services, fake_bot, make_active_client, monkeypatch):
    """Главная: имя; VPN и РФ-доступ; подписка и трафик; устройства — и ряд
    выдачи, раз есть что выдавать."""
    from awgbot.util import timeutil
    cl = make_active_client(tg_id=9130, name="Ксюша", traffic_limit=100 * G)
    d = services.add_device(cl.id, "iPhone")
    services.db.add_traffic_bulk([(d.device_id, 10 * G, 2 * G + 3 * G // 10)])
    services.set_routing_allowed(cl.id, True)
    monkeypatch.setattr(services, "server_ok_cached", lambda: True)
    cl = services.db.get_client(cl.id)
    text, markup = await ch.main_payload(services, cl)
    end = timeutil.parse_iso(cl.period_end)
    lines = text.splitlines()
    assert lines[0] == "👋 Ксюша"
    assert lines[1].startswith("🟢 VPN работает · 🇷🇺 РФ-доступ"), lines[1]
    assert lines[2] == f"💳 Подписка до {timeutil.fmt_date_ui(end)} · 📊 12.3 из 100 ГБ", lines[2]
    assert lines[3] == "📱 Устройств 1 из 3" and len(lines) == 4
    rows = [[b.text for b in r] for r in markup.inline_keyboard]
    assert rows[0] == ["🔗 Ссылка", "🔳 QR", "📄 Файл"] and ["🇷🇺 РФ-доступ", "💳 Подписка"] in rows


async def test_client_main_without_devices_offers_adding_and_no_issue_row(
        services, fake_bot, make_active_client, monkeypatch):
    cl = make_active_client(tg_id=9131, name="Ксюша")
    monkeypatch.setattr(services, "server_ok_cached", lambda: False)
    text, markup = await ch.main_payload(services, cl)
    lines = text.splitlines()
    assert lines[1] == "🔴 VPN не отвечает", "РФ-доступ не выдан — о нём ни слова"
    assert lines[-1] == "📱 Можно добавить до 3 устройств"
    rows = [[b.text for b in r] for r in markup.inline_keyboard]
    assert rows == [["➕ Устройство"], ["💳 Подписка"], ["❓ Помощь"]], rows


async def test_client_main_counts_held_devices_as_having_something_to_issue(
        services, fake_bot, make_active_client):
    """Своих нет, а удерживаемое есть — ряд выдачи нужен: выдавать есть что."""
    owner = make_active_client(tg_id=9132, name="Вася", device_limit=3)
    cl = make_active_client(tg_id=9133, name="Петя")
    dc = services.add_device(owner.id, "Планшет")
    assert services.activate_friend(services.make_device_friendly(dc.device_id), tg_id=9133).ok
    text, markup = await ch.main_payload(services, services.db.get_client(cl.id))
    assert text.splitlines()[-1] == '📱 Устройств 0 из 3 (+1 от профиля <a href="tg://user?id=9132">Вася</a>)'
    assert [b.text for b in markup.inline_keyboard[0]] == ["🔗 Ссылка", "🔳 QR", "📄 Файл"]
