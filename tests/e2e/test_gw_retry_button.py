"""E2E: «🔁 Повторить» под уведомлением о неудачном догоне шлюза.

Notifier ставит кнопку над «Скрыть»; нажатие админом снимает кнопки,
сбрасывает расписание повтора и шлёт команду шлюзу тем же тактом — toast
«Повторяю обновление шлюза»; во время хода — алерт «Обновление уже идёт»;
слоту без неудачи — «Повторять нечего»; у пользователя кнопка — главная его роли.

Цена ошибки: кнопка, после которой команда всё равно ждёт расписания, —
обман; кнопка, плодящая вторую команду во время хода, — гонка с набором.
"""
from __future__ import annotations

import pytest

from awgbot.bot import notifier
from awgbot.bot.callbacks import HideCB, NoteCB
from awgbot.bot.handlers import reply_commands as rc
from awgbot.core import config
from awgbot.runtime import linkserver as ls_pkg
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
BOTH = {"requires": "3.4.0"}


def _rows(markup):
    return [[(b.text, b.callback_data) for b in r] for r in markup.inline_keyboard]


def test_the_retry_hint_becomes_a_button_above_hide():
    rows = _rows(notifier.action_markup(("gw_retry", 2)))
    assert rows == [[("🔁 Повторить", NoteCB(kind="gw_retry", ref=2).pack())],
                    [("Скрыть", HideCB().pack())]], rows


class _Link:
    """Слушатель канала: что ушло шлюзам."""

    def __init__(self):
        self.sent = []

    async def send(self, slot, kind, body):
        self.sent.append((slot, kind, body))
        return True


@pytest.fixture()
def link(monkeypatch):
    srv = _Link()
    monkeypatch.setattr(ls_pkg, "current", lambda: srv)
    return srv


def _failed_catchup(fleet, monkeypatch):
    import random
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1, online=False)
    fleet.start()
    fleet.tick()
    monkeypatch.setattr(random, "uniform", lambda lo, hi: 1.0)
    fleet.hello(1, "3.4.0")
    return fleet.ack(1, "failed", error="GitHub не ответил")


async def _press(services, fake_bot, ref, *, role="admin", client=None, chat=ADMIN):
    note = FakeMessage(text="уведомление", chat_id=chat, user_id=chat, bot=fake_bot)
    cb = FakeCallback(message=note, user_id=chat, bot=fake_bot)
    await rc.on_note_action(cb, NoteCB(kind="gw_retry", ref=ref), FakeState(), services, role=role, client=client)
    return cb, note


async def test_admin_press_strips_the_note_and_sends_the_command_at_once(fleet, fake_bot, link, monkeypatch):
    out = _failed_catchup(fleet, monkeypatch)
    assert out.notes[0].action == ("gw_retry", 1)
    assert fleet.st(1)["next_at"], "догон не ждёт повтора — нечего проверять"
    cb, note = await _press(fleet.s, fake_bot, 1)
    assert cb.answers == [("Повторяю обновление шлюза", False)], cb.answers
    assert ("edit_reply_markup", ADMIN) in fake_bot.records, "кнопки с уведомления не сняты"
    assert [(s, k) for s, k, _ in link.sent] == [(1, "upd")], f"команда не ушла тем же тактом: {link.sent}"
    assert link.sent[0][2]["target"] == "v3.4.1" and link.sent[0][2]["tries"] == 0, link.sent[0][2]
    assert fleet.st(1)["state"] == "sent" and not fleet.st(1).get("next_at")
    assert [t for k, t, _ in note.sent if k == "answer"] == [], "нажатие нарисовало экран — это не навигация"


async def test_press_during_a_run_says_it_is_already_running(fleet, fake_bot, link):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    fleet.slot(2)
    fleet.s.fleet_slot_set(2, {"id": "a1b2c3d4", "state": "failed", "target": "v3.4.1", "tries": 1,
                               "final": False, "next_at": "2030-01-01T00:00:00+00:00", "why": "catchup"})
    fleet.start()
    cb, _ = await _press(fleet.s, fake_bot, 2)
    assert cb.answers == [("Обновление уже идёт", True)], cb.answers
    assert link.sent == [], "во время хода кнопка послала команду вне очереди"
    assert fleet.st(2)["tries"] == 1


async def test_press_for_a_slot_without_a_failure_says_nothing_to_retry(fleet, fake_bot, link):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH))
    fleet.slot(1)
    cb, _ = await _press(fleet.s, fake_bot, 1)
    assert cb.answers == [("Повторять нечего: шлюз не ждёт повтора", True)], cb.answers
    assert link.sent == []


async def test_a_second_press_after_the_command_left_is_nothing(fleet, fake_bot, link, monkeypatch):
    """Старое уведомление с кнопкой в истории чата: команда уже ушла — второго
    нажатия шлюз не замечает."""
    _failed_catchup(fleet, monkeypatch)
    await _press(fleet.s, fake_bot, 1)
    cb, _ = await _press(fleet.s, fake_bot, 1)
    assert cb.answers == [("Повторять нечего: шлюз не ждёт повтора", True)], cb.answers
    assert len(link.sent) == 1, f"команд ушло {len(link.sent)}"


async def test_a_client_pressing_the_admin_button_gets_their_main_menu(fleet, fake_bot, link, monkeypatch,
                                                                      make_active_client):
    _failed_catchup(fleet, monkeypatch)
    cl = make_active_client("Вася", tg_id=5104)
    cb, note = await _press(fleet.s, fake_bot, 1, role="client", client=cl, chat=5104)
    shown = [t for k, t, _ in note.sent if k == "answer"]
    assert shown and shown[-1].startswith("👋 "), shown
    assert link.sent == [], "кнопка пользователя послала команду шлюзу"
    assert fleet.st(1)["next_at"], "пользователь сбросил расписание повтора"
