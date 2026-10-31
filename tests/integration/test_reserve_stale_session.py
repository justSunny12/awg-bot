"""Флаг сеанса канала пары после краха процесса и при мёртвом линке (session:
reserve_session_reset; outbound_return: reserve_force_check, reserve_return_check;
package: reserve_pkg_tick; main/client — сброс на старте).

- флаг `session` лежит в базе и снимается только `finally` сеанса: на старте процесса
  он снимается до подъёма каналов, а `st` второй стороны помечается устаревшим;
- рычаг «вернуть без резерва» и обычный возврат судят по живости, а не по флагу:
  сеанс отрезанного резерва держится до ~23 мин, живость падает за ~1 мин;
- пакет мёртвому резерву не собирается;
- ожидание реплики после возврата без резерва (await_merge) уходит в back_req только по
  живому слову резерва (живость не down, `st` после потери связи), а резерв, пропавший
  снова до реплики, возвращает возврат к ожиданию дома; после потери связи свой `st`
  уходит заново (у второй стороны он устаревший).

Окружение — `mv` из _reserve_move (пара, запись DNS и часы автоматов — модели).

Цена ошибки: после перезагрузки основы при открытом сеансе и мёртвом резерве рычаг
бессрочно отвечает «на связи — верни обычным путём», обычный возврат зависает на
back_req, а такт пакета каждые 5 с собирает новое поколение и копит base-<gen>.db;
аварийный возврат по кворуму при мёртвом резерве навсегда застревает в returning/back_req:
шлюзы держат пересылку и не получают слово возврата, лидер линии — «на другой стороне»,
обновление сервера отказывает, кнопки переезда нет.
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


# ── возврат без резерва (рычаг С9, аварийный по кворуму) при мёртвом линке ──────

def test_the_forced_return_waits_at_home_while_the_hanging_session_belongs_to_a_dead_reserve(mv, monkeypatch):
    """Аварийный возврат по кворуму (check=False): резерв мёртв, а сеанс висит. Ожидание
    реплики дома не уходит в back_req — шлюзы не держат пересылку, слово возврата им идёт."""
    svc = mv.svc
    mv.rsv.gw = False
    rm.go_away(svc)
    rm.st_in(svc, **{"in": "serving"})
    _peer_down(svc, monkeypatch)
    assert svc.reserve_force_home(check=False) == [("take_home",)]
    svc.reserve_stuck_home()                                  # deleteWebhook прошёл
    for _ in range(5):
        mv.clock.advance(301)
        acts = svc.reserve_out_tick()
        assert rm.sent(acts) == [], f"back_req мёртвому резерву: {acts}"
    out = svc.rsv_get("rsv_out")
    assert svc.reserve_out() == "home" and out.get("await_merge") is True, out
    assert svc.reserve_gw_state() == "idle", "шлюзам — «в отъезде», хотя пользователи здесь"


def test_the_forced_return_merges_once_the_reserve_speaks_again(mv, monkeypatch):
    svc = mv.svc
    rm.go_away(svc)
    rm.st_in(svc, **{"in": "serving"})
    _peer_down(svc, monkeypatch)
    svc.reserve_force_home(check=False)
    svc.reserve_stuck_home()
    svc.reserve_liveness_word()                               # линк ожил
    svc.reserve_liveness_tick(10_000.0)
    assert svc.reserve_out_tick() == [], "старый st резерва (до потери связи) принят за живой"
    rm.st_in(svc, **{"in": "serving"})                        # свежий st: гость у него
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "returning" and rm.sent(acts) == ["back_req"], acts


def test_a_reserve_lost_again_before_the_replica_sends_the_forced_return_back_to_waiting(mv, monkeypatch):
    svc = mv.svc
    rm.go_away(svc)
    rm.st_in(svc, **{"in": "serving"})
    svc.reserve_force_home(check=False)
    svc.reserve_stuck_home()
    svc.reserve_out_tick()
    assert svc.reserve_out() == "returning" and svc.rsv_get("rsv_out").get("step") == "back_req", "подготовка"
    _peer_down(svc, monkeypatch)
    acts = svc.reserve_out_tick()
    out = svc.rsv_get("rsv_out")
    assert svc.reserve_out() == "home" and out.get("await_merge") is True and out.get("return_why") == "force", out
    assert ("live",) in acts and rm.sent(acts) == [], acts
    assert svc.reserve_gw_state() == "idle"


def test_a_regular_return_on_back_req_is_not_turned_into_waiting(mv, monkeypatch):
    svc = mv.svc
    rm.go_away(svc)
    rm.st_in(svc, **{"in": "serving"})
    svc.reserve_return_start("button")
    _peer_down(svc, monkeypatch)
    svc.reserve_out_tick()
    assert svc.reserve_out() == "returning", "обычный возврат ушёл в ожидание рычага, которого не было"


def test_the_link_back_after_a_loss_sends_my_st_again(mv):
    """После «недоступна» вторая сторона держит мой st устаревшим (st_stale) до нового: он
    уходит снова, даже если тело не изменилось — иначе её рычаги ждали бы смены поля."""
    svc = mv.svc
    assert svc.reserve_st_if_changed() is not None
    assert svc.reserve_st_if_changed() is None, "подготовка: тело то же — не уходит"
    svc._liveness_up()
    assert svc.reserve_st_if_changed() is not None, "st после возврата связи не ушёл"
