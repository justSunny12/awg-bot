"""Итог аварийного кода в чате роли (handlers/peercode.py): ответ на
сообщение по ходу процесса открытия — «не подошёл» с числом попыток и
открытым вводом, «подошёл, но не поднялось» — с причиной.

Сценарий — `h4` из test_reserve_peer_code; ход процесса — подменённый
rsvzone.open_status.

Цена ошибки: после «не подошёл» ввод закрыт — админ основы в аварию ищет
кнопку заново; ответ «принят» при отказе — ждёт переезда, которого нет.
"""
from __future__ import annotations

import pytest

from awgbot.bot.states import PeerCode
from awgbot.infra import rsvzone
from tests.conftest import FakeMessage, FakeState
from tests.e2e.test_reserve_peer_code import CODE, h4
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import PEER_ADMIN, box

pytestmark = pytest.mark.e2e
rsv = rc.rsv
__all__ = ["box", "h4"]


async def _enter(h4, fake_bot, status: dict, monkeypatch):
    from awgbot.bot.handlers import peercode
    monkeypatch.setattr(rsvzone, "open_status", lambda: dict(status))
    monkeypatch.setattr(rsvzone, "teardown", lambda: "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: "")
    h4.s.__dict__["_rsvopen_stop"] = lambda: None
    msg = FakeMessage(text=CODE, chat_id=PEER_ADMIN, user_id=PEER_ADMIN, bot=fake_bot)
    st = FakeState()
    await st.set_state(PeerCode.value)
    await peercode.code_text(msg, st, h4.s)
    return msg, st


async def test_a_wrong_code_is_answered_in_place_and_the_input_stays_open(h4, fake_bot, monkeypatch):
    msg, st = await _enter(h4, fake_bot, {"step": "fail", "reason": "ключ пакета: не снялся"}, monkeypatch)
    assert msg.deleted, "сообщение с кодом осталось в чате"
    assert msg.sent[-1][1] == "⚠️ Код не подошёл. Осталось попыток: 2 — следующие через час", msg.sent
    assert await st.get_state() == PeerCode.value.state, "после «не подошёл» ввод закрылся"


async def test_a_code_that_fitted_but_did_not_raise_the_clients_says_why(h4, fake_bot, monkeypatch):
    msg, st = await _enter(h4, fake_bot, {"step": "fail", "reason": "зона: veth не поднялся"}, monkeypatch)
    assert "Код подошёл, но клиентов «Beta» поднять не удалось: зона: veth не поднялся" in msg.sent[-1][1], msg.sent
    assert "Приём кода снова открыт" in msg.sent[-1][1]


async def test_a_code_that_took_the_key_is_answered_as_fitting_without_the_reservation(h4, fake_bot, monkeypatch):
    msg, st = await _enter(h4, fake_bot, {"step": "store"}, monkeypatch)
    text = msg.sent[-1][1]
    assert text.startswith("✅ Код подошёл — поднимаю клиентов «Beta»") and "если код не подойдёт" not in text, text
    assert await st.get_state() is None


async def test_a_code_without_a_verdict_in_time_is_accepted_with_the_reservation(h4, fake_bot, monkeypatch):
    msg, st = await _enter(h4, fake_bot, {"step": "launching"}, monkeypatch)
    text = msg.sent[-1][1]
    assert text.startswith("✅ Код принят") and "если код не подойдёт, напишу сюда" in text, text
    assert await st.get_state() is None
