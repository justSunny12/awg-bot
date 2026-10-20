"""Ввод аварийного кода в чате роли админа второго сервера (Т-РН2, Т-РН3):
handlers/peercode.py — кнопка «🔑 Ввести аварийный код», приглашение с
«⬅️ Отмена», код текстом, итог; место роутера и фильтр роли.

Сценарий резерва — `box` из integration/_reserve_vote_common (Н4: основа и
её шлюзы не отвечают, админ основы открывал бот).

Цена ошибки: сообщение с кодом, оставшееся в чате, — код у любого, кто
увидит экран телефона админа; кнопка, открывающая ввод вне Н4, — обещание
переезда, которого не будет; роутер кода после общего роутера роли — текст
с кодом уходит в «тот же экран» и пропадает без ответа.
"""
from __future__ import annotations

import pytest

from awgbot.bot.states import PeerCode
from awgbot.runtime import rsvactions
from awgbot.util import rsvkeys
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import PEER_ADMIN, box

pytestmark = pytest.mark.e2e
rsv = rc.rsv
__all__ = ["box"]

CODE = rsvkeys.code_encode(bytes(range(16)))


@pytest.fixture()
def h4(box, monkeypatch):
    monkeypatch.setattr(rsvactions, "_ctx", {})
    monkeypatch.setattr(box.s, "server_ok", lambda: True)
    box.peer_admin_opened()
    box.primary(False)
    box.tick()
    assert box.s.reserve_code_window()["open"]
    return box


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


# ── место в диспетчере ──────────────────────────────────────────────────────

def test_the_code_router_stands_before_the_role_router_and_is_behind_the_role_filter():
    """Код вводят в роли или общий админ обоих серверов в своём разделе —
    фильтр роли (PeerAdminFilter) на сообщениях и на кнопках."""
    from awgbot.bot.filters import PeerAdminFilter
    from awgbot.bot.handlers import peeradmin, peercode
    from awgbot.bot.routers import routers_for
    order = routers_for("client")
    assert order.index(peercode.router) < order.index(peeradmin.router), "текст с кодом поймает общий ответ роли"
    for observer in (peercode.router.message, peercode.router.callback_query):
        got = [f.callback for f in observer._handler.filters]
        assert len(got) == 1 and isinstance(got[0], PeerAdminFilter), f"обработчики кода без фильтра роли: {got}"


# ── кнопка ──────────────────────────────────────────────────────────────────

async def test_the_role_screen_offers_the_code_only_in_h4(box, fake_bot, monkeypatch):
    from awgbot.bot.handlers import peeradmin
    monkeypatch.setattr(box.s, "server_ok", lambda: True)
    box.primary(False)
    box.hello(1, sees=True)                             # случай 2 — кода не нужно
    msg = FakeMessage(text="/start", chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    await peeradmin.start(msg, box.s)
    assert "🔑 Ввести аварийный код" not in _labels(msg.sent[-1][2])
    box.gone(1)
    box.tick()                                          # Н4
    msg = FakeMessage(text="/start", chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    await peeradmin.start(msg, box.s)
    assert "🔑 Ввести аварийный код" in _labels(msg.sent[-1][2]), "в Н4 на экране роли нет кнопки кода"
    assert "пришли аварийный код" in msg.sent[-1][1]


async def test_the_button_with_the_window_closed_is_an_alert_and_no_input(box, fake_bot):
    from awgbot.bot.handlers import peercode
    nav = FakeMessage(chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    cb, st = FakeCallback(message=nav, user_id=PEER_ADMIN, bot=fake_bot), FakeState()
    await peercode.code_start(cb, st, box.s)
    assert cb.answers and cb.answers[-1][1] is True and "закрыт" in cb.answers[-1][0]
    assert await st.get_state() is None and not nav.sent, "ввод кода открыт вне Н4"


async def test_the_button_in_h4_asks_for_the_code_with_cancel(h4, fake_bot):
    from awgbot.bot.handlers import peercode
    nav = FakeMessage(chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    cb, st = FakeCallback(message=nav, user_id=PEER_ADMIN, bot=fake_bot), FakeState()
    await peercode.code_start(cb, st, h4.s)
    assert await st.get_state() == PeerCode.value.state
    kind, text, markup = nav.sent[-1]
    assert kind == "answer" and "Пришли аварийный код" in text and "удалю сразу" in text
    assert _labels(markup) == ["⬅️ Отмена"]


async def test_cancel_returns_to_the_role_screen(h4, fake_bot):
    from awgbot.bot.handlers import peercode
    nav = FakeMessage(chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    cb, st = FakeCallback(message=nav, user_id=PEER_ADMIN, bot=fake_bot), FakeState()
    await st.set_state(PeerCode.value)
    await peercode.code_cancel(cb, st, h4.s)
    assert await st.get_state() is None
    assert nav.sent[-1][0] == "edit_text" and "Ты — админ «Beta»" in nav.sent[-1][1]
    assert "🔑 Ввести аварийный код" in _labels(nav.sent[-1][2])


# ── код текстом ─────────────────────────────────────────────────────────────

async def test_the_code_message_is_deleted_before_the_answer_and_goes_to_the_process(h4, fake_bot):
    from awgbot.bot.handlers import peercode
    msg = FakeMessage(text=CODE, chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    st = FakeState()
    await st.set_state(PeerCode.value)
    await peercode.code_text(msg, st, h4.s)
    assert msg.deleted, "сообщение с кодом осталось в чате"
    kinds = [r[0] for r in fake_bot.records]
    assert kinds.index("delete") < kinds.index("answer"), f"ответ ушёл раньше удаления кода: {kinds}"
    assert "Код принят" in msg.sent[-1][1]
    assert len(h4.launched) == 1 and h4.launched[0]["code"] == CODE.replace("-", "")
    assert await st.get_state() is None


async def test_a_garbled_code_is_answered_and_the_input_stays_open(h4, fake_bot):
    from awgbot.bot.handlers import peercode
    bad = CODE[:-1] + ("0" if CODE[-1] != "0" else "1")
    msg = FakeMessage(text=bad, chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    st = FakeState()
    await st.set_state(PeerCode.value)
    await peercode.code_text(msg, st, h4.s)
    assert msg.deleted and "Осталось попыток: 2" in msg.sent[-1][1]
    assert await st.get_state() == PeerCode.value.state, "после неразобранного кода ввод закрылся"
    assert not h4.launched


async def test_after_the_window_closes_the_text_is_answered_as_not_needed(h4, fake_bot):
    from awgbot.bot.handlers import peercode
    h4.hello(1, sees=True)                              # шлюз вернулся «вижу» — переезд не нужен
    msg = FakeMessage(text=CODE, chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    st = FakeState()
    await st.set_state(PeerCode.value)
    await peercode.code_text(msg, st, h4.s)
    assert msg.deleted and "переезд не нужен" in msg.sent[-1][1]
    assert not h4.launched
