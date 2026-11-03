"""Пульт у основы в боте (bot/middleware.py, bot/keyboards/reserve.py, карточка резерва,
handlers/settings/reserve/move.py): Telegram бота у основы, её пользователи на резерве.

- пользователям, гостям и новым по приглашению — «🔧 Бот на техработах — VPN работает»
  (кнопка — всплывашка, сообщение — ответ не чаще раза в минуту), админу — меню как
  обычно; без пульта и в госте — как прежде;
- карточка основы: строка «🎛 Telegram бота здесь…», кнопки «↩️ Вернуть пользователей
  сюда» (рычаг «вернуть без резерва» — его диалог и его отказы) и «📤 Отдать Telegram
  резерву» (вместо «⚠️ Вернуть без …»); резерв на связи и обычный возврат возможен —
  вместо рычага «↩️ Вернуть пользователей»; «отдаю…» — строкой;
- «📤 Отдать Telegram резерву»: отказ всплывашкой с причиной, при живом линке — слово
  `pult_back` второй стороне и всплывашка «Отдаю Telegram резерву»; в госте
   кнопка не работает;
- карточка резерва: строка «└ ⚠️ Telegram бота «Beta» у него — пользователи здесь».

Нажатия идут через настоящий диспетчер роли.

Цена ошибки: пользователи без ответа — думают, что VPN сломан, и переподключаются;
пользователи с обычным меню — жмут продления и правки, а бот их не запишет (следует);
админ, которому тоже ответили «бот на техработах», — пульт есть, а нажать нечего; кнопка
отдачи без отказа при мёртвом линке — Telegram «отдан» в никуда.
"""
from __future__ import annotations

import pytest

from awgbot.bot.callbacks import Menu, RsvCB, SetCB
from awgbot.bot.routers import make_dispatcher
from awgbot.bot.texts.reserve import pult as pt
from awgbot.core import access_cache, config
from awgbot.domain.services.reserve import guest as guest_svc
from awgbot.runtime import pairlink
from awgbot.util import timeutil
from tests.e2e.test_reserve_move_ui import _card, _edits, _kind, _labels, _toast, gui, ui
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.screens import base, harness

pytestmark = pytest.mark.e2e
rsv = rc.rsv
mv = rm.mv
__all__ = ["ui", "gui"]

USER = 5000
LEVER = "↩️ Вернуть пользователей сюда"
GIVE = "📤 Отдать Telegram резерву"
FORCE = "⚠️ Вернуть без «Beta»"


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def _pult(services, on: bool = True) -> None:
    services.rsv_update("rsv_out", state="away" if on else "home", pult_at=timeutil.now_iso() if on else "")


# ── пользователи ────────────────────────────────────────────────────────────

@pytest.fixture()
def world(services, make_active_client):
    access_cache.invalidate_all()
    make_active_client(name="Вера", tg_id=USER)
    yield services
    access_cache.invalidate_all()


async def _press(services, uid: int, cb, *, role: str = "client") -> list:
    dp = make_dispatcher(role, services, services.db, reattach=True)
    session = harness.StubSession(uid, base.NOW, base.PEERS)
    rec = await harness.run(dp, session, uid=uid, name="U", steps=[("press", cb)], services=services)
    return rec.calls


async def _say(services, uid: int, *texts: str) -> list:
    dp = make_dispatcher("client", services, services.db, reattach=True)
    session = harness.StubSession(uid, base.NOW, base.PEERS)
    rec = await harness.run(dp, session, uid=uid, name="U", steps=[("text", t) for t in texts], services=services)
    return rec.everything


def _toasts(calls) -> list[str]:
    return [c.head for c in calls if c.head.startswith("~")]


def _sends(calls) -> list[str]:
    return [c.body for c in calls if c.head.startswith("+ send")]


async def test_a_user_button_gets_the_maintenance_alert(world):
    _pult(world)
    calls = await _press(world, USER, Menu(action="main"))
    assert _toasts(calls) == [f"~! {pt.USER_MAINTENANCE}"], [c.head for c in calls]
    assert not _sends(calls) and not [c for c in calls if c.head.startswith("= edit")], "пользователю нарисован экран"


async def test_a_user_message_gets_one_maintenance_answer_per_minute(world):
    _pult(world)
    calls = await _say(world, USER, "привет", "ау")
    assert _sends(calls) == [pt.USER_MAINTENANCE], _sends(calls)


async def test_a_stranger_with_an_invite_gets_the_maintenance_answer(world):
    _pult(world)
    calls = await _say(world, 8888, "/start CabcABC12345")
    assert _sends(calls) == [pt.USER_MAINTENANCE], _sends(calls)


async def test_the_admin_gets_the_menu_as_usual(world):
    _pult(world)
    calls = await _press(world, config.ADMIN_ID, Menu(action="main"))
    assert pt.USER_MAINTENANCE not in " ".join(_toasts(calls) + _sends(calls)), "админу — «бот на техработах»"
    assert [c for c in calls if c.head.startswith(("= edit", "+ send"))], "меню админа не нарисовано"


@pytest.mark.parametrize("state", ["home", "leaving"])
async def test_without_the_pult_a_user_gets_the_menu(world, state):
    world.rsv_update("rsv_out", state=state, pult_at="2026-11-01T10:00:00+05:00")   # остаток отметки вне эпизода
    calls = await _press(world, USER, Menu(action="main"))
    assert pt.USER_MAINTENANCE not in " ".join(_toasts(calls)), "без пульта — «бот на техработах»"
    assert [c for c in calls if c.head.startswith(("= edit", "+ send"))], "меню не нарисовано"


async def test_the_bot_on_the_reserve_does_not_answer_maintenance(world):
    _pult(world)
    calls = await _press(world, USER, Menu(action="main"), role="guest")
    assert pt.USER_MAINTENANCE not in " ".join(_toasts(calls))


# ── карточка основы ─────────────────────────────────────────────────────────

def _gone(ui, minutes: float = 11) -> None:
    rm.go_away(ui.svc, kind="dead")
    ui.svc.db.set_state("last_server_ok", "0")
    ui.svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(minutes))
    ui.svc._lv.state = "down"


def _take(ui) -> None:
    ui.svc.reserve_pult_taken()


async def test_the_card_shows_the_pult_line_and_its_two_buttons(ui):
    _gone(ui)
    _text, labels = await _card(ui)
    assert FORCE in labels and LEVER not in labels and GIVE not in labels, labels
    _take(ui)
    text, labels = await _card(ui)
    assert "└ 🎛 Telegram бота здесь с " in text and "бот на «Beta» не отвечал — пользователям отвечаю «бот на техработах»" in text, text
    assert LEVER in labels and GIVE in labels and FORCE not in labels, labels
    assert labels.index(LEVER) < labels.index(GIVE)


async def test_with_the_reserve_back_the_card_offers_the_normal_return_instead_of_the_lever(ui):
    _gone(ui)
    _take(ui)
    rm.st_in(ui.svc, **{"in": "serving"})
    ui.svc._lv.state = "ok"
    ui.svc.rsv_update("rsv_out", can_return=True)
    _text, labels = await _card(ui)
    assert "↩️ Вернуть пользователей" in labels and LEVER not in labels and GIVE in labels, labels


async def test_the_lever_button_opens_the_lever_dialog_or_says_why_not(ui):
    _gone(ui, minutes=5)
    _take(ui)
    t = _toast(await ui.run.press(RsvCB(action="force")))
    assert t.show_alert and "меньше 10 мин" in t.text, t.text
    _gone(ui)
    _take(ui)
    e = _edits(await ui.run.press(RsvCB(action="force")))
    assert e and "Вернуть пользователей без «Beta»?" in e[0].text
    assert _labels(e[0]) == ["⬅️ Отмена", "⚠️ Да, вернуть"]


async def test_giving_telegram_back_is_refused_while_the_link_is_silent(ui, monkeypatch):
    sent = []

    async def send(kind, body=None):
        sent.append((kind, body))
        return True
    monkeypatch.setattr(pairlink, "send", send)
    _gone(ui)
    _take(ui)
    t = _toast(await ui.run.press(RsvCB(action="pult_give")))
    assert t.show_alert and t.text == "«Beta» не на связи — отдать Telegram некому", t.text
    assert sent == []


async def test_giving_telegram_back_sends_the_word_and_shows_giving(ui, monkeypatch):
    sent = []

    async def send(kind, body=None):
        sent.append((kind, body))
        return True
    monkeypatch.setattr(pairlink, "send", send)
    _gone(ui)
    _take(ui)
    rm.st_in(ui.svc, **{"in": "serving"})
    ui.svc._lv.state = "ok"
    calls = await ui.run.press(RsvCB(action="pult_give"))
    assert _toast(calls).text == pt.GIVE_TOAST
    assert sent == [("pult_back", {"at": ui.svc.rsv_get("rsv_out")["pult_at"]})], sent
    e = _edits(calls)
    assert e and "└ ⏳ отдаю Telegram «Beta»" in e[0].text, "карточка не сказала, что Telegram отдаётся"


async def test_giving_does_not_work_in_the_bot_on_the_reserve(gui):
    calls = await gui.run.press(RsvCB(action="pult_give"))
    assert [_kind(c) for c in calls] == ["AnswerCallbackQuery"] and _toast(calls).text == guest_svc.GUEST_REFUSED


# ── карточка резерва ────────────────────────────────────────────────────────

async def test_the_reserve_card_says_the_telegram_of_its_guest_is_at_the_primary(ui):
    ui.svc.reserve_set_in("serving", kind="dead", tg_taken=True, taken_at=timeutil.now_iso())
    text, _labels_ = await _card(ui)
    assert "Telegram бота «Beta» у него" not in text
    rm.st_in(ui.svc, out="away", tg_pult="2026-11-01T10:00:00+05:00")
    text, _labels_ = await _card(ui)
    assert "└ ⚠️ Telegram бота «Beta» у него — пользователи здесь" in text, text
    ui.svc.reserve_pult_back_in("2026-11-01T10:00:00+05:00")
    text, _labels_ = await _card(ui)
    assert "Telegram бота «Beta» у него" not in text


async def test_the_card_screen_has_no_settings_entry_for_the_pult(ui):
    """Пульт — не настройка: в корне настроек раздела не прибавилось."""
    e = _edits(await ui.run.press(SetCB(sec="root")))
    assert e and not any("Telegram" in x and "резерв" in x.lower() for x in _labels(e[0]))
