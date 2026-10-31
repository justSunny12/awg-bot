"""Потерянный `back` возврата и пакет при занятой зоне (outbound_return: _home_back_resend;
package: reserve_pkg_bad, reserve_pkg_tick).

- основа дома, а живой резерв в `st` всё ещё releasing — её `back` потерялся (шаг relay
  повторял его и сдался): `back` снова, не чаще раза в минуту; резерв сказал иное, не на
  связи или идёт возврат без резерва — молчит;
- пакет, отвергнутый резервом («зона занята», обрыв сессии), не собирается заново каждые
  5 с: повтор с отступом (10 с, удваивается до 5 мин); отступ снимают `pkg_ok`, новая
  сессия и `st` резерва без зоны после «зона занята».

Окружение — `mv` из _reserve_move (пара, запись DNS и часы автоматов — модели).

Цена ошибки: резерв в releasing навечно — держит зону и данные основы, отвергает каждый
новый пакет, переезд к нему закрыт; пакет каждые 5 с — новое поколение, полный снимок
базы по каналу и base-<gen>.db на диске без конца.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.reserve import package
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


@pytest.fixture()
def back_home(mv):
    """Возврат кончился по сроку шага relay: основа дома, `wiped` не пришёл."""
    svc = mv.svc
    svc.reserve_set_out("returning", kind="button", return_why="auto", step="relay")
    svc._return_done([])
    assert svc.reserve_out() == "home"
    return mv


def test_a_live_reserve_still_releasing_gets_back_again_once_a_minute(back_home):
    svc = back_home.svc
    rm.st_in(svc, **{"in": "releasing"})
    assert rm.sent(svc.reserve_out_tick()) == ["back"], "резерв в releasing, а back не повторён"
    back_home.clock.advance(30)
    assert "back" not in rm.sent(svc.reserve_out_tick()), "back чаще раза в минуту"
    back_home.clock.advance(31)
    assert rm.sent(svc.reserve_out_tick()) == ["back"]
    rm.st_in(svc, **{"in": "relay"})                          # резерв принял back
    back_home.clock.advance(61)
    assert "back" not in rm.sent(svc.reserve_out_tick())


def test_no_back_to_a_reserve_that_is_down(back_home, monkeypatch):
    svc = back_home.svc
    rm.st_in(svc, **{"in": "releasing"})
    svc.rsv_update("rsv_peer_state", session=False)
    assert "back" not in rm.sent(svc.reserve_out_tick())


def test_no_back_while_the_forced_return_waits_for_the_replica(back_home):
    svc = back_home.svc
    rm.st_in(svc, **{"in": "releasing"})
    svc.rsv_update("rsv_out", await_merge=True)
    assert "back" not in rm.sent(svc.reserve_out_tick()), "back вместо back_req — реплика резерва пропала бы"


# ── пакет, отвергнутый резервом ─────────────────────────────────────────────

def _built_ticks(mv, seconds: int, reason: str) -> list[int]:
    gens = []
    for _ in range(seconds // 5):
        built = mv.svc.reserve_pkg_tick()
        if built is not None:
            gens.append(int(built["gen"]))
            mv.svc.reserve_pkg_bad(int(built["gen"]), reason)
        mv.clock.advance(5)
    return gens


@pytest.mark.parametrize("reason", ["зона занята", "сессия оборвалась"])
def test_a_refused_package_is_retried_with_a_growing_pause(mv, reason):
    mv.svc.reserve_pkg_mark()
    mv.clock.advance(11)
    gens = _built_ticks(mv, 600, reason)
    assert 2 <= len(gens) <= 8, f"за 10 мин собрано {len(gens)} пакетов (каждые 5 с — 120)"


def test_the_pause_never_exceeds_five_minutes(mv):
    mv.svc.reserve_pkg_mark()
    mv.clock.advance(11)
    gens = _built_ticks(mv, 3600, "зона занята")
    assert len(gens) >= 3600 // package.PKG_RETRY_MAX_S - 1, gens


def test_the_reserve_free_again_ends_the_pause(mv):
    svc = mv.svc
    rm.st_in(svc, **{"in": "releasing"})
    svc.reserve_pkg_mark()
    mv.clock.advance(11)
    assert len(_built_ticks(mv, 200, "зона занята")) >= 2
    rm.st_in(svc, **{"in": "idle"})
    svc.reserve_pkg_mark()
    mv.clock.advance(11)
    assert svc.reserve_pkg_tick() is not None, "резерв освободил зону, а пакет ждёт отступа"


def test_a_new_session_and_pkg_ok_end_the_pause(mv):
    svc = mv.svc
    svc.reserve_pkg_mark()
    mv.clock.advance(11)
    _built_ticks(mv, 200, "сессия оборвалась")
    svc.reserve_session_opened({})
    svc.reserve_pkg_mark()
    mv.clock.advance(11)
    built = svc.reserve_pkg_tick()
    assert built is not None, "новая сессия — пакет ждёт отступа прежней"
    svc.reserve_pkg_ok(int(built["gen"]), built["fp"])
    assert svc.__dict__.get("_rsv_pkg_retry_at") is None
