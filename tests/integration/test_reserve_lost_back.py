"""Потерянный `back` возврата и пакет при занятой зоне (outbound_return: _home_back_resend;
package: reserve_pkg_bad, reserve_pkg_tick).

- основа дома, а живой резерв в `st` всё ещё releasing — её `back` потерялся (шаг relay
  повторял его и сдался): `back` снова, не чаще раза в минуту; резерв сказал иное, не на
  связи или идёт возврат без резерва — молчит;
- срыв переезда после передачи бота: `back` ушёл в мёртвый канал (резерв был недоступен —
  потому и срыв), а живой `st` резерва после перехода домой говорит taking под этот переезд
  (по моему K, тот же пакет) — `back` снова раз в минуту; taking по точкам или коду, другой
  пакет, гость с Telegram по принудительному захвату, `st` до срыва или резерв не на связи —
  молчит;
- пакет, отвергнутый резервом («зона занята», обрыв сессии), не собирается заново каждые
  5 с: повтор с отступом (10 с, удваивается до 5 мин); отступ снимают `pkg_ok`, новая
  сессия и `st` резерва без зоны после «зона занята».

Окружение — `mv` из _reserve_move (пара, запись DNS и часы автоматов — модели).

Цена ошибки: резерв в releasing навечно — держит зону и данные основы, отвергает каждый
новый пакет, переезд к нему закрыт; после срыва — резерв держит зону и живого гостя до
своих сроков (до 37 мин), правки гостя не приходят, отметка С9 доживает до «не ждать?»;
`back` чужому открытию (по точкам, коду) снёс бы подъём, который основа должна принять; пакет каждые 5 с — новое поколение, полный снимок
базы по каналу и base-<gen>.db на диске без конца.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.reserve import package

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


# ── срыв переезда: back ушёл в мёртвый канал ─────────────────────────────────

@pytest.fixture()
def failed_move(mv):
    """Переезд сорвался после передачи бота (резерв был недоступен — потому и срыв): основа
    дома, её `back` ушёл в мёртвый канал; резерв потом говорит в `st`, что всё ещё открывает
    зону под этот переезд (taking по моему K, пакет того же поколения)."""
    svc = mv.svc
    rm.to_step(svc, "released")
    acts = svc._move_fail("резерв не взял право отвечать — вебхук не поставлен", released=True, steps=("released",))
    assert svc.reserve_out() == "home" and "back" in rm.sent(acts), "условие: срыв сказал back один раз"
    return mv


def _taking_mine(svc, **over) -> None:
    rm.st_in(svc, **{"in": "taking", "in_by": "k", "in_kind": "button", "pkg": {"gen": 7, "fp": ""}, **over})


def test_a_live_reserve_still_opening_my_failed_move_gets_back_again_once_a_minute(failed_move):
    svc = failed_move.svc
    _taking_mine(svc)
    assert rm.sent(svc.reserve_out_tick()) == ["back"], "резерв в taking под сорвавшийся переезд, а back не повторён"
    failed_move.clock.advance(30)
    assert "back" not in rm.sent(svc.reserve_out_tick()), "back чаще раза в минуту"
    failed_move.clock.advance(31)
    assert rm.sent(svc.reserve_out_tick()) == ["back"]
    _taking_mine(svc, **{"in": "relay"})                      # резерв принял back
    failed_move.clock.advance(61)
    assert "back" not in rm.sent(svc.reserve_out_tick())
    assert svc.reserve_out() == "home"


@pytest.mark.parametrize("over,why", [
    ({"in_by": "share"}, "открытие по точкам кворума мимо меня — не мой эпизод, back снёс бы чужой подъём"),
    ({"in_by": "code"}, "открытие по коду мимо меня — не мой эпизод"),
    ({"pkg": {"gen": 6, "fp": ""}}, "зона под другим пакетом — не мой эпизод"),
    ({"in_tg": "forced"}, "гость забрал Telegram сам — на back резерв ответил бы taken, повторять нечего"),
])
def test_no_back_to_a_reserve_whose_taking_is_not_my_failed_move(failed_move, over, why):
    svc = failed_move.svc
    _taking_mine(svc, **over)
    assert "back" not in rm.sent(svc.reserve_out_tick()), why


def test_no_back_on_a_st_received_before_the_failure(failed_move):
    svc = failed_move.svc
    _taking_mine(svc)
    out = svc.rsv_get("rsv_out")
    svc.rsv_update("rsv_peer_state", at_ts=float(out["at_ts"]) - 1)
    assert "back" not in rm.sent(svc.reserve_out_tick()), "st о ходе переезда до срыва — back на него уже ушёл"


def test_no_back_to_a_reserve_that_went_down_after_the_failure(failed_move):
    svc = failed_move.svc
    _taking_mine(svc)
    svc.rsv_update("rsv_peer_state", session=False)
    assert "back" not in rm.sent(svc.reserve_out_tick())
