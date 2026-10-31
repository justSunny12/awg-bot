"""Флаг сеанса канала пары после краха процесса и при мёртвом линке (session:
reserve_session_reset; outbound_return: reserve_force_check, reserve_return_check;
package: reserve_pkg_tick; main/client — сброс на старте).

- флаг `session` лежит в базе и снимается только `finally` сеанса: на старте процесса
  он снимается до подъёма каналов, а `st` второй стороны помечается устаревшим;
- рычаг «вернуть без резерва» и обычный возврат судят по живости, а не по флагу:
  сеанс отрезанного резерва держится до ~23 мин, живость падает за ~1 мин;
- пакет мёртвому резерву не собирается.

Окружение — `mv` из _reserve_move (пара, запись DNS и часы автоматов — модели).

Цена ошибки: после перезагрузки основы при открытом сеансе и мёртвом резерве рычаг
бессрочно отвечает «на связи — верни обычным путём», обычный возврат зависает на
back_req, а такт пакета каждые 5 с собирает новое поколение и копит base-<gen>.db.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


def _peer_down(svc, monkeypatch) -> None:
    """Линк пары молчит: счётчик rx не растёт — живость доходит до `down`, флаг сеанса цел."""
    svc.__dict__.pop("_reserve_liveness", None)
    monkeypatch.setattr(svc, "_pair_rx", lambda: None)
    t = 1000.0
    for _ in range(20):
        t += 5
        svc.reserve_liveness_tick(t)
    assert svc.reserve_peer_seen() == "down" and svc.reserve_online(), "подготовка: сеанс есть, живость down"


def test_the_start_of_the_process_drops_the_session_flag_and_marks_the_st_stale(mv):
    svc = mv.svc
    assert svc.reserve_online(), "подготовка: флаг сеанса прошлого процесса в базе"
    svc.reserve_session_reset()
    ps = svc.reserve_peer_state()
    assert not svc.reserve_online() and ps.get("st_stale") is True, ps
    assert ps.get("name") == "Beta", "сброс стёр то, что известно о второй стороне"


def test_the_reset_without_a_pair_writes_nothing(services):
    services.reserve_session_reset()
    assert services.rsv_get("rsv_peer_state") == {}


def test_the_main_process_resets_the_pair_session_before_the_channels_and_polling():
    src = (Path(__file__).resolve().parents[2] / "awgbot" / "runtime" / "main" / "client.py").read_text(encoding="utf-8")
    at = src.index("services.reserve_session_reset()")
    assert at < src.index("pairlink.ensure(services)") and at < src.index("PollControl("), \
        "флаг сеанса снят после подъёма канала пары или поллинга"


def test_the_lever_is_possible_when_the_session_of_a_dead_reserve_still_hangs(mv, monkeypatch):
    svc = mv.svc
    rm.go_away(svc, kind="dead")
    _peer_down(svc, monkeypatch)
    svc.rsv_update("rsv_peer_state", down_since=timeutil.to_iso(timeutil.shift(timeutil.now(), -11 * 60)))
    assert svc.reserve_force_check() == "", "рычаг закрыт флагом сеанса мёртвого резерва"


def test_the_regular_return_is_refused_while_the_reserve_is_down(mv, monkeypatch):
    svc = mv.svc
    rm.go_away(svc)
    _peer_down(svc, monkeypatch)
    why = svc.reserve_return_check()
    assert "не на связи" in why, f"обычный возврат к мёртвому резерву разрешён: {why!r}"


def test_no_package_is_built_for_a_reserve_that_is_down(mv, monkeypatch):
    svc = mv.svc
    _peer_down(svc, monkeypatch)
    svc.reserve_pkg_mark()
    mv.clock.advance(11)
    assert svc.reserve_pkg_tick() is None, "пакет собран мёртвому резерву"
