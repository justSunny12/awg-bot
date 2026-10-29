"""Экраны раздела «🛟 Резерв» с журналом данных (bot/texts/reserve/section.py по
данным reserve_card_data / reserve_section_data / reserve_force_dialog_data):
Т-Ж1/Т-Ж2 под «📤 Твои пользователи: на …», Т-Ж3 (ждём правки бота на резерве),
Т-Ж7 (диалог «вернуть без резерва»), Т-Ж8 («▸ подробнее» карточки — каким путём
идут данные), Т-Ж9 («▸ подробнее» раздела).

Экран собирается так же, как его показывает хендлер (reserve_screen). Мир — пара
на двух базах (tests/integration/_reserve_jrn_duo.py).

Цена ошибки: карточка без строки следования — админ не видит, что данные
здесь отстают от бота на резерве и возврат подождёт; «жду реплику» при
журнале — обещание слияния, которого не будет; диалог С9 со старой фразой —
админ не знает, что при споре останутся здешние значения; «▸ подробнее» без
пути — непонятно, почему у второй стороны без журнала уходит весь пакет.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from awgbot.bot.handlers.settings.reserve.card import reserve_screen
from awgbot.bot.texts.reserve import section as st_t
from awgbot.infra import awg

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import Duo, checked

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

_START, _FUTURE = "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00"


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    yield d
    d.close()


def _screen(svc) -> str:
    return asyncio.run(reserve_screen(svc))[0]


def _following(duo) -> None:
    """Журнал включён, переезд, основа следует и применила одно событие бота на резерве."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following()
    with duo.as_guest():
        duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
        (kind, body), = [m for m in duo.g.guest_jrn_tick() if m[0] == "jev"]
    duo.a.reserve_jrn_in(kind, checked(kind, body))


def test_tj1_the_card_says_the_data_follow_the_reserves_bot_with_the_time(duo):
    _following(duo)
    text = _screen(duo.a)
    lines = text.split("\n")
    i = next(n for n, ln in enumerate(lines) if ln.startswith("📤 Твои пользователи: на «Beta»"))
    assert lines[i + 1].startswith("└ данные здесь следуют за ботом на «Beta» · актуальны на "), lines[i:i + 2]
    assert "🟡" not in lines[i + 1]


def test_tj2_a_lag_over_a_minute_replaces_tj1(duo):
    _following(duo)
    keep = dict(duo.a.reserve_peer_state().get("jrn_keep") or {})
    keep["guest_last"] = duo.a.db.jrn_meta()["applied"] + 3
    duo.a.rsv_update("rsv_peer_state", jrn_keep=keep)
    duo.a.__dict__["_follow_lag_since"] = time.monotonic() - 61
    text = _screen(duo.a)
    assert "└ 🟡 данные здесь догоняют бот на «Beta» · актуальны на " in text, text
    assert "следуют за ботом" not in text


def test_tj1_is_not_shown_at_home(duo):
    duo.journal_on()
    text = _screen(duo.a)
    assert "следуют за ботом" not in text and "догоняют" not in text


def test_tj3_waiting_for_the_reserves_edits_with_the_journal(duo):
    _following(duo)
    duo.a.rsv_update("rsv_out", await_merge=True)
    text = _screen(duo.a)
    assert "└ 🟡 изменения из бота на «Beta» придут, когда он выйдет на связь" in text, text
    assert "жду реплику" not in text


def test_tj3_without_the_journal_the_old_line_stays(mv):
    rc.pair_row(mv.svc)
    rm.go_away(mv.svc, kind="dead")
    mv.svc.rsv_update("rsv_out", await_merge=True)
    text = _screen(mv.svc)
    assert "└ ⏳ жду реплику «Beta» — изменения из неё применятся, когда он выйдет на связь" in text, text


def test_tj8_the_details_say_the_data_go_as_edits(duo):
    duo.journal_on()
    text = _screen(duo.a)
    assert "Данные для переезда уходят к «Beta» по мере изменений, раз в сутки — полный слепок контрольно" in text, text


def test_tj8_a_peer_without_the_journal_gets_the_old_path_with_the_reason(mv):
    rc.pair_row(mv.svc)
    rm.st_in(mv.svc)                                     # выпуск 4.0.0, возможностей журнала нет
    text = _screen(mv.svc)
    assert "Данные для переезда уходят к «Beta» полным слепком после каждого изменения — версия бота на «Beta» " \
           "не поддерживает журнал правок" in text, text
    assert "по мере изменений" not in text


# Т-Ж9 («прочитать правки резерв не может, но видит…») с экрана снята 29.10.2026 — живёт в README


def test_tj7_the_force_dialog_with_the_journal(duo):
    _following(duo)
    text = st_t.force_dialog(duo.a.reserve_force_dialog_data())
    assert "Изменения от бота на «Beta» за это время придут, когда он выйдет на связь; если здесь успеешь внести " \
           "конфликтующие со снимком с «Beta» правки — останутся они, список пришлю" in text, text
    assert "вольётся" not in text


def test_tj7_without_the_journal_the_old_phrase_stays(mv):
    rc.pair_row(mv.svc)
    rm.go_away(mv.svc, kind="dead")
    text = st_t.force_dialog(mv.svc.reserve_force_dialog_data())
    assert "Изменения с «Beta» за это время применятся, когда он выйдет на связь" in text, text
