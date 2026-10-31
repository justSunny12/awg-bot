"""Правки по ревью — очередь уведомлений агента о резерве (domain/gateway/reserve/store.py,
vote.py): Т-РА3 при открытии пересылки не выбрасывается до первой попытки доставки,
приходит и по документу основы `away`, не дублируется по слову `state{serving}`.

Обвязка — `gw`/`host` из test_gw_reserve_agent (база, rsvlink и запись DNS подменены);
`host.run` исполняет открытие и закрытие пересылки, как runtime/guestlink; часы такта —
настоящий monotonic (отметка первой попытки ставится им же).

Цена ошибки: переезд при мёртвом аплинке — Т-РА3 уходит в мёртвый Telegram, а очередь
выбросила его тем же тактом (пересылка ещё не открыта) — админ шлюза не узнаёт, что
пользователи у резерва; по документу `away` «объявлено» записано без уведомления — Т-РА3
не приходит вовсе; повтор, пока первое ещё в пути, — два одинаковых уведомления со звуком.
"""
from __future__ import annotations

import time

import pytest

from awgbot.domain.gateway.reserve import store as store_mod
from tests.unit.test_gw_reserve_agent import _checked, gw, host

pytestmark = pytest.mark.unit
__all__ = ["gw", "host"]


def _notes(acts) -> list:
    return [n for a in acts or [] if a and a[0] == "notify" for n in a[1]]


@pytest.fixture()
def quiet_tg(gw, monkeypatch):
    monkeypatch.setattr(gw, "reserve_tgvia_tick", lambda now=None: [])


def _pending(gw) -> list:
    return list(gw.reserve_section().get("pending_notes") or [])


def test_the_moved_note_survives_the_tick_that_opens_the_forward(gw, host, quiet_tg):
    host.link = True
    host.fact = True
    cur = gw.reserve_section()
    cur["reserve_state"], cur["reserve_move"] = "serving", 3
    store_mod._write(cur, gw._RSV_STORE)
    acts = gw.reserve_watch_tick(now=time.monotonic())
    assert [a[0] for a in acts][:2] == ["open", "notify"] and len(_notes(acts)) == 1
    assert len(_pending(gw)) == 1, "Т-РА3 выброшен из очереди тем же тактом — повтора при мёртвом аплинке не будет"
    host.run(acts)                                            # пересылка открыта, отправка не дошла
    again = gw.reserve_notes_retry(now=time.monotonic() + store_mod.NOTE_RETRY_S + 1)
    assert len(_notes(again)) == 1, "недоставленный Т-РА3 не повторён через минуту"


def test_the_document_away_brings_the_moved_note(gw, host, quiet_tg):
    host.link = True
    cur = gw.reserve_section()
    cur["reserve_move"] = 3
    store_mod._write(cur, gw._RSV_STORE)
    gw.reserve_apply(_checked(gw.conf, state="away", move=3))
    assert not gw.reserve_section().get("moved_told"), "«объявлено» записано при применении документа без уведомления"
    acts = gw.reserve_watch_tick(now=time.monotonic())
    assert [a[0] for a in acts][:1] == ["open"], acts
    assert len(_notes(acts)) == 1, "по документу away Т-РА3 не пришёл"
    host.run(acts)
    assert gw.reserve_section().get("moved_told") == 3
    assert not _notes(gw.reserve_watch_tick(now=time.monotonic() + 5)), "Т-РА3 повторён тактом при открытой пересылке"


def test_the_moved_note_is_not_repeated_while_the_first_is_in_flight(gw, host):
    host.link = True
    host.fact = True
    acts = gw.reserve_state_in("serving", 5)
    assert len(_notes(acts)) == 1
    host.run(acts)                                            # notify ещё в пути (on_sent не звали)
    assert not _notes(gw.reserve_notes_retry(now=time.monotonic() + 1)), "второе Т-РА3, пока первое в пути"


def test_a_note_whose_forward_closed_before_the_retry_is_dropped(gw, host):
    host.link = True
    host.fact = True
    acts = gw.reserve_state_in("serving", 5)
    host.run(acts)
    host.open = []                                            # переезд кончился до повтора
    assert gw.reserve_notes_retry(now=time.monotonic() + store_mod.NOTE_RETRY_S + 1) == []
    assert _pending(gw) == [], "устаревший Т-РА3 остался в очереди"
