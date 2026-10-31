"""Переходы автомата «мои пользователи» (domain/services/reserve/outbound*.py) между
потоками: обработчики слов канала пары (`taken`, `released` — to_thread) и такт
(`reserve_out_tick`) читают `rsv_out`, решают и пишут — без общего замка каждый мог
записать свой переход поверх чужого.

Цена ошибки: `taken` резерва и истечение шага передачи бота в одном окне — такт
срывает переезд (home, `back` резерву: тот сносит гостя) поверх уже состоявшегося
away — пользователи и бот нигде; истечение шага merge, пока слияние ещё идёт, —
повторный `back_req`, вторая реплика и второе слияние той же базы поверх первого;
истечение шага back_req поверх только что начатого слияния — шаг restart затёрт,
перезапуск не наступает.
"""
from __future__ import annotations

import threading

import pytest

from awgbot.domain.services.reserve import outbound

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import notices, sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


class _Gate:
    """Вход в шаг такта (`_tick_leaving` / `_tick_returning` — `out` уже прочитан тактом)
    в потоке `name` останавливается до `go`: окно между «прочитал» и «записал» держится
    открытым, пока второй поток пишет своё."""

    def __init__(self, svc, name: str, method: str) -> None:
        self.svc, self.name, self.method = svc, name, method
        self.real = getattr(svc, method)
        self.entered, self.go = threading.Event(), threading.Event()
        self.once = False

    def __enter__(self):
        def slow(*a, **k):
            if threading.current_thread().name == self.name and not self.once:
                self.once = True
                self.entered.set()
                self.go.wait(3)
            return self.real(*a, **k)
        setattr(self.svc, self.method, slow)
        return self

    def __exit__(self, *exc):
        setattr(self.svc, self.method, self.real)


def _run(svc, name: str, fn, *args) -> tuple[threading.Thread, list]:
    result: list = []
    t = threading.Thread(target=lambda: result.append(fn(*args)), name=name)
    t.start()
    return t, result


def test_taken_during_the_expired_release_step_is_exactly_one_transition(mv):
    """Такт прочитал шаг release с вышедшим сроком; пока он решал, пришло `taken`
    (поток канала): away. Такт обязан увидеть новое состояние и ничего не срывать —
    ни home, ни `back` резерву, ни «переезд не состоялся» админу."""
    svc = mv.svc
    rm.to_step(svc, "release", wait=outbound.RELEASED_WAIT_S)
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    with _Gate(svc, "tick", "_tick_leaving") as g:
        t, tick = _run(svc, "tick", svc.reserve_out_tick)
        assert g.entered.wait(2)
        taken = svc.reserve_taken_in()
        assert svc.reserve_out() == "away" and len(notices(taken)) == 1
        g.go.set()
        t.join(3)
    assert svc.reserve_out() == "away" and svc.reserve_poll() == "away", "срыв по сроку лёг поверх состоявшегося taken"
    assert sent(tick[0]) == [] and notices(tick[0]) == [], f"такт сорвал переезд после taken: {tick[0]}"


def test_the_expired_released_step_and_taken_do_not_tell_the_admin_twice(mv):
    """Шаг released вышел, Telegram сказал «вебхук у резерва» — такт ведёт в away; в то же
    окно `taken` тоже ведёт в away: переход один, уведомление о переезде — одно."""
    svc = mv.svc
    rm.to_step(svc, "released")
    svc.reserve_set_poll("away", why="webhook")
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    with _Gate(svc, "tick", "_tick_leaving") as g:
        t, tick = _run(svc, "tick", svc.reserve_out_tick)
        assert g.entered.wait(2)
        taken = svc.reserve_taken_in()
        g.go.set()
        t.join(3)
    out = svc.rsv_get("rsv_out")
    assert out["state"] == "away" and out.get("step") == "", f"такт дописал шаг переезда в запись away: {out}"
    assert len(notices(taken)) + len(notices(tick[0])) == 1, "о переезде сказано дважды"
    assert tick[0] == [], f"такт шага released после taken что-то сделал: {tick[0]}"


def test_taken_and_the_expiry_started_together_make_exactly_one_transition(mv):
    """`taken` и такт с вышедшим сроком стартуют одновременно (барьер): кто бы ни
    успел первым, переход один — либо away без `back` и без срыва, либо home с `back`
    и без away; смесь (home + away, `back` после taken) — пользователи нигде."""
    import sys
    svc = mv.svc
    rm.to_step(svc, "release", wait=outbound.RELEASED_WAIT_S)
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    gate = threading.Barrier(2)
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        def word():
            gate.wait(3)
            return svc.reserve_taken_in()
        t, taken = _run(svc, "word", word)
        gate.wait(3)
        tick = svc.reserve_out_tick()
        t.join(3)
    finally:
        sys.setswitchinterval(old)
    state = svc.reserve_out()
    moved = len(notices(taken[0])) + len(notices(tick))
    if state == "away":
        assert sent(tick) == [] and moved == 1 and svc.reserve_poll() == "away", (tick, taken)
    else:
        assert state == "home" and sent(tick) == ["back"] and taken[0] == [] and svc.reserve_poll() == "mine", (tick, taken)


@pytest.fixture()
def ret(mv):
    """Возврат на шаге back_req; слияние — заглушка, которой тест управляет."""
    mv.rsv.gw = False
    rm.go_away(mv.svc)
    rm.st_in(mv.svc, **{"in": "serving"})
    mv.svc.reserve_return_start("button")
    assert mv.svc.rsv_get("rsv_out")["step"] == "back_req"
    mv.blob = rm.snapshot_blob(mv.svc)
    return mv


def test_the_expiry_of_the_merge_step_does_not_ask_for_a_replica_while_the_merge_is_running(ret):
    """Слияние идёт дольше срока шага merge (большая база, медленный диск): такт не должен
    просить реплику снова — вторая реплика легла бы вторым слиянием поверх первого; и
    повторная реплика в это время — не второе слияние. Итог первого слияния — restart."""
    svc = ret.svc
    started, finish = threading.Event(), threading.Event()
    calls: list = []

    def merge(blob, gen):
        calls.append(gen)
        started.set()
        finish.wait(3)
        return {"lost": []}

    svc._return_merge = merge
    t, first = _run(svc, "word", svc.reserve_released_in, ret.blob)
    assert started.wait(2) and svc.rsv_get("rsv_out")["step"] == "merge"
    ret.clock.advance(outbound.RELEASED_WAIT_S + 1)
    tick = svc.reserve_out_tick()
    assert sent(tick) == [], f"срок шага merge вышел при идущем слиянии — такт попросил реплику снова: {tick}"
    assert svc.rsv_get("rsv_out")["step"] == "merge", "шаг merge сброшен при идущем слиянии"
    again = svc.reserve_released_in(ret.blob)
    assert again == [] and calls == [7], "повторная реплика при идущем слиянии начала второе"
    finish.set()
    t.join(3)
    assert first[0] == [("restart",)] and svc.rsv_get("rsv_out")["step"] == "restart"


def test_the_expiry_of_back_req_does_not_overwrite_a_merge_that_has_just_begun(ret):
    """Такт прочитал back_req с вышедшим сроком; реплика пришла и слилась (restart) раньше,
    чем он записал повтор, — повтор не должен лечь поверх: шаг restart остаётся, `back_req`
    резерву не уходит (иначе вторая реплика и слияние поверх первого)."""
    svc = ret.svc
    svc._return_merge = lambda blob, gen: {"lost": []}
    ret.clock.advance(outbound.RELEASED_WAIT_S + 1)
    with _Gate(svc, "tick", "_tick_returning") as g:
        t, tick = _run(svc, "tick", svc.reserve_out_tick)
        assert g.entered.wait(2)
        assert svc.reserve_released_in(ret.blob) == [("restart",)]
        g.go.set()
        t.join(3)
    assert svc.rsv_get("rsv_out")["step"] == "restart", "повтор back_req по сроку затёр шаг restart"
    assert sent(tick[0]) == [], f"back_req ушёл поверх слияния: {tick[0]}"
