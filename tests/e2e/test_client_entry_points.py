"""E2E: входы в экраны клиента и гостя помимо кнопок — кнопка из старого
меню (обработчик устаревших) и коды F… через тот же /start, что и ссылки.

Цена ошибки: устаревшая кнопка без обработчика крутит часики и молчит;
ссылка, принятая за код, — «такого кода нет» вместо экрана, а код, принятый
за ссылку, — устройство не уходит в держание.

Экраны по ссылкам (/start sub|rf|dev-<id>, в том числе правкой живого меню и
на чужое устройство) и главная клиента во всех состояниях — в эталоне
(cl.link.*, gst.link.*, cl.main.*). Устаревшую кнопку эталон не снимает (его
сторож запрещает уход в обработчик устаревших) — она здесь.
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


# ── коды через /start у клиента ──────────────────────────────────────────────

async def _start(services, fake_bot, cl, payload):
    msg = FakeMessage(text=f"/start {payload}", chat_id=cl.tg_id, user_id=cl.tg_id, bot=fake_bot)
    await ch.start_client_with_code(msg, _cmd(payload), cl, services, FakeState())
    return msg


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


# ── коды через /start у гостя ────────────────────────────────────────────────

async def _guest_start(services, fake_bot, guest, payload):
    msg = FakeMessage(text=f"/start {payload}", chat_id=guest.tg_id, user_id=guest.tg_id, bot=fake_bot)
    await fh.friend_start_with_code(msg, _cmd(payload), guest, services, FakeState())
    return [s for s in msg.sent if s[0] == "answer"]


async def test_guest_second_code_through_start_still_activates(services, fake_bot, make_active_client):
    owner = make_active_client(tg_id=9122, name="Вася", device_limit=3)
    _, guest = _lend(services, owner, 99122, "Тел")
    d2 = services.add_device(owner.id, "Ноут")
    await _guest_start(services, fake_bot, guest, services.make_device_friendly(d2.device_id))
    assert services.db.get_device(d2.device_id).holder_client_id == guest.id
