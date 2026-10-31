"""Отметка С9 (`rsv_c9`, journal/c9) — под эпоху гостя, которую основа сменила сейчас:

- эпоха снова взята без released (второй эпизод подряд: срыв, зависший вебхук,
  «вернуть без резерва»), а отметка прошлого эпизода не снята фактом — она
  заменяется отметкой новой эпохи (новый момент заведения, F/upto — по новой
  точке взятия), строкой в журнал; правки гостя прошлой эпохи, не пришедшие
  хвостом, больше не ждём (как при «не ждать»);
- правки нового гостя приходят хвостом С9 (перенос, jack), а не отказом jstale;
- повтор такта той же эпохи отметку не трогает (перенесённое на месте);
- рычаг «вернуть без резерва» заводит отметку тем же правилом, что роль эпохи.

Цена ошибки: отметка прошлого эпизода остаётся, правки людей у нового гостя
отвергаются jstale — при встрече они не переносятся и пропадают из базы основы
молча; плашка «жду события» показывает давно истёкшее ожидание чужой эпохи, а
снятие «не ждать» снимает и то, что ещё придёт.
"""
from __future__ import annotations

import logging
import shutil

import pytest

from awgbot.infra import awg
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import Duo, sends
from tests.integration.test_reserve_jrn_tail_pair import _base, _names, _to_main

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

_START, _FUTURE = "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00"


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    yield d
    d.close()


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def _fresh_package(duo) -> None:
    """Резерв без гостя (прошлый остановлен, его база и журнал убраны) принял свежий пакет
    основы под её нынешним K."""
    if duo.gdb is not None:
        duo.gdb.close()
        duo.gdb = duo.g = None
        for p in duo.tmp.glob("guest.db*"):
            p.unlink()
        shutil.rmtree(duo.tmp / "gvar" / "jrn", ignore_errors=True)
    duo.b.rsv_update("rsv_in", state="idle", kind="")
    reply = duo.deliver_pkg(duo.snapshot(deliver=False))
    assert reply[0] == "pkg_ok", f"условие: резерв принял пакет: {reply}"


def _episode(duo) -> int:
    """Гость открылся сам (авария) по свежему пакету, основа ушла в следование, затем
    сняла зависший вебхук без рычага — эпоха здесь без released. Эпоха гостя."""
    _fresh_package(duo)
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following() and duo.a._pkg_journal(), "условие: следование журналом"
    guest_e = int(duo.a.db.jrn_meta()["epoch"])
    duo.a.rsv_update("rsv_peer_state", down_since=_ago(31))
    duo.a.reserve_stuck_home()
    duo.a.reserve_epoch_sync()
    m = duo.a.db.jrn_meta()
    assert (m["holder"], m["role"]) == ("self", "holder"), m
    return guest_e


def test_a_second_epoch_taken_without_released_replaces_the_stale_c9_mark(duo, caplog):
    _base(duo)
    e1 = _episode(duo)
    first = dict(duo.a.rsv_get("rsv_c9"))
    assert first.get("e") == e1, f"условие: отметка первого эпизода: {first}"
    duo.clock.advance(5)
    e2 = _episode(duo)
    assert e2 != e1, "условие: второй эпизод — другая эпоха гостя"
    c9 = duo.a.rsv_get("rsv_c9")
    m = duo.a.db.jrn_meta()
    assert c9.get("e") == e2, f"отметка прошлого эпизода не заменена под новую эпоху гостя: {c9}"
    assert (c9.get("F"), c9.get("upto"), c9.get("moved")) == (m["fork_i"], m["fork_i"], 0), c9
    assert float(c9["at_ts"]) >= float(first["at_ts"]), c9
    assert c9.get("k") == (duo.tmp / "grun" / "k").read_text(encoding="utf-8"), "ключ отметки — не ключ нового гостя"
    assert any(f"эпоха {e1}" in r.getMessage() and f"эпоху {e2}" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING), "замена отметки без строки в журнал"
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
        msgs = duo.g.guest_jrn_tick()
    at_a = _to_main(duo, msgs)
    assert [k for k, _ in sends(at_a)] == ["jack"], f"правки второго гостя не приняты хвостом С9: {at_a}"
    assert "Ольга" in _names(duo.a.db), _names(duo.a.db)
    assert int(duo.a.rsv_get("rsv_c9")["upto"]) == duo.gdb.jrn_last_i()


def test_the_same_epoch_on_a_repeated_tick_keeps_the_mark(duo):
    _base(duo)
    _episode(duo)
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O")
        msgs = duo.g.guest_jrn_tick()
    _to_main(duo, msgs)
    before = dict(duo.a.rsv_get("rsv_c9"))
    assert int(before.get("moved") or 0) > 0, f"условие: часть хвоста перенесена: {before}"
    duo.a.reserve_c9_arm(int(before["e"]))
    assert duo.a.rsv_get("rsv_c9") == before, "повтор той же эпохи сбросил перенесённое"


def test_the_lever_replaces_a_stale_mark_by_the_same_rule(duo):
    """«Вернуть без резерва» поверх отметки прошлого эпизода — отметка эпохи текущего гостя."""
    _base(duo)
    e1 = _episode(duo)
    _fresh_package(duo)
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    e2 = int(duo.a.db.jrn_meta()["epoch"])
    assert duo.a.reserve_following() and e2 != e1 and duo.a.rsv_get("rsv_c9").get("e") == e1, "условие"
    rm.automatic(duo.a)
    duo.a.db.set_state("last_server_ok", "0")
    duo.a.rsv_update("rsv_peer_state", session=False, down_since=_ago(11))
    duo.a.reserve_force_home()
    c9 = duo.a.rsv_get("rsv_c9")
    assert c9.get("e") == e2, f"рычаг оставил отметку прошлого эпизода: {c9}"
    assert c9.get("F") == duo.a.db.jrn_meta()["fork_i"], c9
