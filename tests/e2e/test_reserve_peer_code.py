"""Ввод аварийного кода в чате роли админа второго сервера (Т-РН2, Т-РН3):
handlers/peercode.py — кнопка «🔑 Ввести аварийный код», приглашение с
«⬅️ Отмена», код текстом, итог; место роутера и фильтр роли.

Сценарий резерва — `box` и `Cand` из integration/_reserve_vote_common: окно
кода открывает кандидат кворума — вне решения (свой «не вижу», шлюзы молчат)
или в ходе решения (большинство «нет», лидер молчит, точек нет); админ
основы открывал бот.

Цена ошибки: сообщение с кодом, оставшееся в чате, — код у любого, кто
увидит экран телефона админа; кнопка, открывающая ввод при закрытом окне, — обещание
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
from tests.integration._reserve_vote_common import PEER_ADMIN, Cand, box, code_window_by_decision, code_window_outside

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
    box.cand = Cand(box)
    code_window_outside(box.cand)
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

@pytest.mark.parametrize("opener", [code_window_outside, code_window_by_decision], ids=["outside", "decision"])
async def test_the_role_screen_offers_the_code_only_while_the_candidate_holds_the_window(box, fake_bot, monkeypatch,
                                                                                          opener):
    """Пока окна нет (свой «вижу») — кнопки кода и строки 🆘 на экране роли
    нет; кандидат открыл окно (вне решения или в ходе решения без точек) —
    есть обе."""
    from awgbot.bot.handlers import peeradmin
    monkeypatch.setattr(box.s, "server_ok", lambda: True)
    box.peer_admin_opened()
    c = Cand(box)
    c.tick()
    msg = FakeMessage(text="/start", chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    await peeradmin.start(msg, box.s)
    assert "🔑 Ввести аварийный код" not in _labels(msg.sent[-1][2]), "кнопка кода без окна кода"
    assert "пришли аварийный код" not in msg.sent[-1][1]
    opener(c)
    msg = FakeMessage(text="/start", chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    await peeradmin.start(msg, box.s)
    assert "🔑 Ввести аварийный код" in _labels(msg.sent[-1][2]), "окно кода открыто, а на экране роли нет кнопки кода"
    assert "а голосов для переезда не хватает — пришли аварийный код резерва" in msg.sent[-1][1], msg.sent[-1][1]


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
    assert kind == "edit_text" and "Пришли аварийный код" in text and "удалю сразу" in text, \
        "приглашение к вводу — не на месте экрана роли (второе сообщение в чате)"
    assert _labels(markup) == ["⬅️ Отмена"]
    assert (await st.get_data()).get("prompt") == [nav.chat.id, nav.message_id], "место приглашения не запомнено"


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
    assert msg.deleted and "не похоже на код" in msg.sent[-1][1], msg.sent
    assert "Осталось попыток" not in msg.sent[-1][1], "опечатка посчитана попыткой подбора"
    assert await st.get_state() == PeerCode.value.state, "после неразобранного кода ввод закрылся"
    assert not h4.launched


async def test_a_command_instead_of_the_code_closes_the_input_without_a_try(h4, fake_bot):
    from awgbot.bot.handlers import peercode
    msg = FakeMessage(text="/start", chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    st = FakeState()
    await st.set_state(PeerCode.value)
    await peercode.code_command(msg, st, h4.s)
    assert await st.get_state() is None
    assert "Ты — админ «Beta»" in msg.sent[-1][1] and "🔑 Ввести аварийный код" in _labels(msg.sent[-1][2])
    assert not h4.launched and not h4.s._code_tries(dict(h4.s.rsv_get("rsv_view").get("code") or {}))


async def test_after_the_result_the_role_screen_comes_back_in_place_of_the_prompt(h4, fake_bot):
    from awgbot.bot.handlers import peercode
    nav = FakeMessage(chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    cb, st = FakeCallback(message=nav, user_id=PEER_ADMIN, bot=fake_bot), FakeState()
    await peercode.code_start(cb, st, h4.s)
    msg = FakeMessage(text=CODE, chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    await peercode.code_text(msg, st, h4.s)
    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edits and "Ты — админ «Beta»" in edits[-1][2], f"экран роли не вернулся на место приглашения: {fake_bot.records}"


async def test_after_the_window_closes_the_text_is_answered_as_not_needed(h4, fake_bot):
    from awgbot.bot.handlers import peercode
    h4.hello(1)
    h4.cand.view(1, "see")                              # шлюз вернулся «вижу» — переезд не нужен
    h4.cand.tick()
    msg = FakeMessage(text=CODE, chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    st = FakeState()
    await st.set_state(PeerCode.value)
    await peercode.code_text(msg, st, h4.s)
    assert msg.deleted and "переезд не нужен" in msg.sent[-1][1]
    assert not h4.launched
