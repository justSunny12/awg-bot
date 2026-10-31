"""С9 в карточке основы: «⚠️ Вернуть пользователей без «Beta»» — кнопка только
когда рычаг возможен, диалог с «Отменой» первой и опасной кнопкой, тексты
по режиму записи, подтверждение запускает рычаг; строка «⏳ жду реплику» в
карточке, пока реплика резерва не влилась; в боте основы на резерве кнопки
не работают.

Нажатия идут через настоящий диспетчер роли (`_Run` из test_reserve_move_ui).

Цена ошибки: кнопка при живом резерве — админ отбирает Telegram у бота,
который ещё обслуживает пользователей; диалог без слов о записи — человек не
знает, что менять руками; рычаг в боте основы на резерве — гость отбирает
Telegram сам у себя.
"""
from __future__ import annotations

import pytest

from awgbot.bot.callbacks import RsvCB
from awgbot.bot.texts.reserve import section as st_t
from awgbot.domain.services.reserve import guest as guest_svc
from awgbot.util import timeutil
from tests.e2e.test_reserve_move_ui import _card, _edits, _kind, _labels, _toast, gui, ui
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP

pytestmark = pytest.mark.e2e
rsv = rc.rsv
mv = rm.mv
__all__ = ["ui", "gui"]

FORCE = "⚠️ Вернуть без «Beta»"


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def _gone(ui, minutes: float = 11) -> None:
    rm.go_away(ui.svc, kind="dead")
    ui.svc.db.set_state("last_server_ok", "0")
    ui.svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(minutes))


async def test_the_card_offers_the_lever_only_when_the_reserve_is_gone_ten_minutes(ui):
    _text, labels = await _card(ui)
    assert FORCE not in labels, "рычаг, когда пользователи и так здесь"
    _gone(ui, minutes=5)
    _text, labels = await _card(ui)
    assert FORCE not in labels, "рычаг раньше 10 минут"
    _gone(ui)
    _text, labels = await _card(ui)
    assert FORCE in labels, labels
    ui.svc.rsv_update("rsv_peer_state", session=True)
    _text, labels = await _card(ui)
    assert FORCE not in labels, "рычаг при резерве на связи"


async def test_the_dialog_in_manual_mode_tells_what_to_change_and_puts_cancel_first(ui):
    _gone(ui)
    e = _edits(await ui.run.press(RsvCB(action="force")))
    assert e, "диалог не показан"
    text = e[0].text
    assert "Вернуть пользователей без «Beta»?" in text and "смени у провайдера запись" in text and OWN_IP in text, text
    assert rc.HOST_A in text and "Пока «Beta» не на связи, пересылки устройств с него нет" in text, text
    assert _labels(e[0]) == ["⬅️ Отмена", "⚠️ Да, вернуть"], _labels(e[0])
    assert not ui.svc.rsv_get("rsv_out").get("await_merge"), "диалог сам запустил рычаг"


async def test_the_dialog_in_automatic_mode_says_the_record_returns_by_itself(ui):
    _gone(ui)
    rm.automatic(ui.svc)
    text = _edits(await ui.run.press(RsvCB(action="force")))[0].text
    assert "вернётся на" in text and "смени у провайдера" not in text, text


async def test_the_dialog_is_refused_with_the_reason_when_the_lever_is_impossible(ui):
    calls = await ui.run.press(RsvCB(action="force"))
    t = _toast(calls)
    assert t.show_alert and "не у резерва" in t.text and not _edits(calls)


async def test_confirming_runs_the_lever_and_redraws_the_card(ui):
    _gone(ui)
    rm.automatic(ui.svc)
    calls = await ui.run.press(RsvCB(action="force_yes"))
    assert ui.svc.rsv_get("rsv_out").get("await_merge") is True and ui.dns.sets == [OWN_IP]
    assert _toast(calls).text == "Запись возвращается, Telegram забираю"
    assert _edits(calls), "карточка не перерисована"


async def test_a_confirmation_that_came_too_late_is_refused_loudly(ui):
    _gone(ui)
    ui.svc.rsv_update("rsv_peer_state", session=True)          # резерв вышел на связь, пока диалог висел
    ui.svc._lv.state = "ok"                                     # живым словом, не одним флагом сеанса
    t = _toast(await ui.run.press(RsvCB(action="force_yes")))
    assert t.show_alert and "на связи" in t.text
    assert not ui.svc.rsv_get("rsv_out").get("await_merge")


async def test_the_card_says_it_waits_for_the_replica_while_the_mark_stands(ui):
    ui.svc.reserve_set_out("home", await_merge=True, kind="dead")
    text, labels = await _card(ui)
    assert "⏳ жду реплику «Beta»" in text and "изменения из неё применятся" in text, text
    assert FORCE not in labels
    ui.svc.reserve_set_out("home", await_merge=False)
    text, _labels_ = await _card(ui)
    assert "жду реплику" not in text


def test_the_dialog_text_without_a_record_has_no_record_sentence():
    text = st_t.force_dialog({"name": "Beta", "since": "", "record": "", "own": "", "manual": True})
    assert "запись" not in text and "«Beta» не на связи. " in text, text
    assert not text.rstrip().endswith(".")


@pytest.mark.parametrize("act", ["force", "force_yes"])
async def test_the_lever_does_not_work_in_the_bot_on_the_reserve(gui, act):
    calls = await gui.run.press(RsvCB(action=act))
    assert [_kind(c) for c in calls] == ["AnswerCallbackQuery"] and _toast(calls).text == guest_svc.GUEST_REFUSED
