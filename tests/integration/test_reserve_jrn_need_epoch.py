"""`jneed` основы по её линии вне ожидания С9 (journal/route: _keep_serve_base) —
резерв досылает записи подряд с её номера из хранилища её событий, иначе из
журнала гостя на диске, и только эпохи, которая держит просимый номер:
старшей из начавшихся не позже него (начала — первые номера эпох в обоих
хранилищах и эпоха поднятого гостя с номера после его точки). `jneed` гостя
о событиях основы (_keep_serve_guest) — по тому же правилу эпохи, из
хранилища её событий.

Хранилище держит сегменты нескольких эпох: после смены эпохи (передача бота,
точка расхождения) прежняя эпоха с теми же номерами остаётся на диске до
снимка. Цена ошибки: основа, просившая правки бота на резерве (эпоха гостя),
получает свои же прежние записи под теми же номерами — их следующая эпоха
заменила с точки расхождения; правки людей на резерве подменены старыми, а
номер считается досланным; гость в следовании, получивший прежнюю запись
вместо новой, расходится с основой молча — его база несёт заменённое.
"""
from __future__ import annotations

import pytest

from awgbot.util.rsvjrn import event
from awgbot.util.rsvjrn.apply import Event
from awgbot.util.rsvjrn.segments import Store

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

LINE = "9f3c1a7e0b2d4c55"
K = bytes(range(32))


def _rec(i: int, e: int) -> bytes:
    return event.seal_event(K, LINE, Event(i=i, e=e, t="devices", op="U", k={"id": i}, c={"name": f"e{e}"},
                                           at="2026-10-27T14:03:11"))


@pytest.fixture()
def world(mv, tmp_path, monkeypatch):
    keep, guest = Store(tmp_path / "keep"), Store(tmp_path / "guest")
    keep.path.mkdir()
    guest.path.mkdir()
    svc = mv.svc
    monkeypatch.setattr(svc, "reserve_keep_store", lambda: keep)
    monkeypatch.setattr(svc, "guest_jrn_store", lambda: guest)
    svc.rsv_put("rsv_jrn_in", {"line": LINE, "e": 2, "snap_i": 0, "cb": 0, "last": 7})
    mv.keep, mv.guest = keep, guest
    return mv


def _jev(svc, frm: int) -> dict:
    acts = svc.reserve_jrn_in("jneed", {"line": LINE, "from": frm})
    jev = [a[2] for a in acts if a[:2] == ("send", "jev")]
    assert jev, f"jneed не обслужен из хранилища: {acts}"
    return jev[0]


def test_her_own_lost_events_are_served_from_the_keep_store(world):
    world.keep.append_events([_rec(i, 2) for i in range(1, 8)])
    jev = _jev(world.svc, 5)
    assert (jev["e"], jev["from"], jev["n"]) == (2, 5, 3)


def test_the_same_numbers_of_an_older_epoch_in_the_keep_store_are_not_served(world):
    """Основа сменила эпоху с точки 4: в хранилище e2 по 7 и e3 с 5 — под номером 5
    уходит запись e3, не прежняя e2 (сегмент e2 читается первым)."""
    world.keep.append_events([_rec(i, 2) for i in range(1, 8)])
    world.keep.append_events([_rec(i, 3) for i in range(5, 9)])
    world.svc.rsv_update("rsv_jrn_in", e=3)
    jev = _jev(world.svc, 5)
    assert (jev["e"], jev["from"], jev["n"]) == (3, 5, 4), "под теми же номерами ушло событие прежней эпохи"


def test_the_guest_epoch_wins_over_her_old_events_of_the_same_numbers(world):
    """Гость (эпоха 3) писал с точки 4, гостя уже нет, его журнал на диске; у меня в
    хранилище — прежние события основы e2 с теми же номерами. Ей — записи гостя."""
    world.keep.append_events([_rec(i, 2) for i in range(1, 8)])
    world.guest.append_events([_rec(i, 3) for i in range(5, 7)])
    jev = _jev(world.svc, 5)
    assert (jev["e"], jev["from"], jev["n"]) == (3, 5, 2), "основе ушли её прежние записи вместо правок гостя"


def test_nothing_of_the_wanted_epoch_goes_to_the_guest_and_not_as_an_old_record(world, monkeypatch):
    """Эпоха гостя известна (он поднят и сказал о себе: эпоха 3 с точки 4), записей этой
    эпохи с номера нет ни в одном хранилище — не старая запись, а просьба гостю, как
    прежде."""
    world.keep.append_events([_rec(i, 2) for i in range(1, 8)])
    monkeypatch.setattr(world.svc, "_keep_guest_int", lambda key: {"e": 3, "fork": 4}.get(key, 0))
    acts = world.svc.reserve_jrn_in("jneed", {"line": LINE, "from": 5})
    assert acts == [("guest", "jneed", {"line": LINE, "from": 5})], acts


def test_numbers_before_the_guest_fork_stay_with_her_epoch(world, monkeypatch):
    """Точка гостя — 7 (он открылся по всем событиям у меня): номера до неё держит эпоха
    основы, хотя гость старше, — её потерянные при пропаже питания события ей из
    хранилища, а не просьба гостю, у которого их в журнале нет."""
    world.keep.append_events([_rec(i, 2) for i in range(1, 8)])
    world.guest.append_events([_rec(i, 3) for i in range(8, 10)])
    monkeypatch.setattr(world.svc, "_keep_guest_int", lambda key: {"e": 3, "fork": 7}.get(key, 0))
    jev = _jev(world.svc, 5)
    assert (jev["e"], jev["from"], jev["n"]) == (2, 5, 3)
    jev = _jev(world.svc, 8)
    assert (jev["e"], jev["from"], jev["n"]) == (3, 8, 2), "после точки — записи гостя"


def _guest_jev(svc, frm: int) -> dict:
    acts = svc.reserve_guest_jrn_word("jneed", {"line": LINE, "from": frm})
    jev = [a[2] for a in acts if a[:2] == ("guest", "jev")]
    assert jev, f"jneed гостя не обслужен из хранилища: {acts}"
    return jev[0]


def test_the_guest_gets_her_events_from_the_keep_store(world):
    world.keep.append_events([_rec(i, 2) for i in range(1, 8)])
    jev = _guest_jev(world.svc, 5)
    assert (jev["e"], jev["from"], jev["n"]) == (2, 5, 3)


def test_the_guest_does_not_get_the_same_numbers_of_an_older_epoch(world):
    """Основа сменила эпоху с точки 4: в хранилище e2 по 7 и e3 с 5 — гостю, просившему
    с 5, уходит запись e3, а не заменённая e2 (сегмент e2 читается первым)."""
    world.keep.append_events([_rec(i, 2) for i in range(1, 8)])
    world.keep.append_events([_rec(i, 3) for i in range(5, 9)])
    world.svc.rsv_update("rsv_jrn_in", e=3)
    jev = _guest_jev(world.svc, 5)
    assert (jev["e"], jev["from"], jev["n"]) == (3, 5, 4), "гостю ушло событие прежней эпохи под тем же номером"
    jev = _guest_jev(world.svc, 2)
    assert (jev["e"], jev["from"], jev["n"]) == (2, 2, 6), "номера до точки — эпохи, которая их держит"


def test_the_guest_gets_nothing_when_no_epoch_holds_the_number(world):
    """Хранилище начинается с 5 — с 2 досылать нечего, а не запись другой эпохи."""
    world.keep.append_events([_rec(i, 3) for i in range(5, 9)])
    assert world.svc.reserve_guest_jrn_word("jneed", {"line": LINE, "from": 2}) == []
