"""Срыв приёма на резерве: пересылка хвоста прошлого возврата при новом открытии (inbound_back:
_relay_yield, _relay_restore, _tick_relay; inbound: _take_fail, отказ запуска _go_launch) и
отметка «срыв идёт» (inbound: _take_fail).

- новое открытие снимает пересылку (DNAT увёл бы пользователей нового клона на основу), но
  помнит её; открытие сорвалось — на любом шаге или уже на запуске — пересылка возвращается
  с прежними сроком и адресом, такт relay перерисовывает файервол и сбрасывает conntrack;
- открытие без пересылки до него и после неё пересылку не выдумывает: отметка прошлого
  эпизода не тянется;
- отметка «срыв идёт» живёт в памяти процесса: процесс, убитый посреди срыва (и отметка
  прежнего выпуска в базе), не оставляет taking навсегда — новый процесс срыв доделывает;
  второй срыв, пока идёт первый, — пустой.

Окружение — `host` из test_reserve_hosting (зона, процессы и systemctl подставные).

Цена ошибки: устройства, сидящие после прошлого возврата на адресе резерва, теряют VPN
при живой основе — из-за чужого срыва переезда (разовый ключ не пришёл, процесс открытия
упал, основа ушла из сеанса); отметка срыва в базе — после рестарта посреди срыва резерв
вечно в taking: зона занята, новые пакеты отвергаются, переезды к нему закрыты.
"""
from __future__ import annotations

import pytest

from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_hosting import host  # noqa: F401 — фикстура

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

TAIL = "203.0.113.9:45877"


def _relay(svc) -> str:
    until = timeutil.to_iso(timeutil.shift(timeutil.now(), 5 * 86400))
    svc.reserve_set_in("relay", relay_to=TAIL, relay_until=until, relay_at=timeutil.now_iso(), step="")
    return until


def test_a_failed_open_brings_the_tail_relay_back(host):
    svc = host.svc
    until = _relay(svc)
    assert svc.reserve_go_req_in(7, "button") == ""
    assert svc.reserve_in() == "taking" and not svc.rsv_get("rsv_in").get("relay_to"), "подготовка: хвост уступил"
    host.zone.status = {"step": "fail", "reason": "разовый ключ не пришёл"}
    acts = svc.reserve_in_tick()
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "relay" and inn.get("relay_to") == TAIL and inn.get("relay_until") == until, inn
    assert host.zone.names()[-1] == "relay" and host.zone.calls[-1] == ("relay", TAIL), host.zone.calls
    assert host.zone.names().index("wipe") < len(host.zone.names()) - 1, "пересылка до снятия зоны — снятие её стёрло бы"
    assert rm.sent(acts) == ["down"] and ("fw",) in acts, acts
    acts = svc.reserve_in_tick()
    assert acts == [("fw",), ("relay_flush",)], f"DNAT хвоста не перерисован после возврата пересылки: {acts}"
    assert svc.reserve_in_tick() == [], "перерисовка повторилась"


def test_a_failed_launch_brings_the_tail_relay_back(host):
    svc = host.svc
    _relay(svc)
    host.spawn.popen_error = OSError("нет python")
    why = svc.reserve_go_req_in(7, "button")
    assert "нет python" in why
    assert svc.reserve_in() == "relay" and svc.rsv_get("rsv_in").get("relay_to") == TAIL
    assert svc.reserve_in_tick() == [("fw",), ("relay_flush",)]


def test_an_open_without_a_tail_relay_brings_none_after_a_failure(host):
    svc = host.svc
    _relay(svc)
    assert svc.reserve_go_req_in(7, "button") == ""
    host.zone.status = {"step": "fail", "reason": "x"}
    svc.reserve_in_tick()                                     # хвост вернулся
    svc.reserve_set_in("idle", step="", relay_to="", relay_until="")   # срок хвоста вышел
    host.zone.calls.clear()
    host.clock.advance(3600)
    svc.rsv_update("rsv_in", fail_at="")                      # пауза после срыва — не о том
    assert svc.reserve_go_req_in(7, "button") == ""
    host.zone.status = {"step": "fail", "reason": "y"}
    svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and "relay" not in host.zone.names(), \
        f"пересылка прошлого эпизода вернулась: {host.zone.calls}"


# ── отметка «срыв идёт» ─────────────────────────────────────────────────────

def test_a_fail_mark_left_in_the_base_by_a_killed_process_does_not_hold_taking_forever(host):
    svc = host.svc
    assert svc.reserve_go_req_in(7, "button") == ""
    svc.rsv_update("rsv_in", failing=True)                    # процесс убит посреди срыва (прежний выпуск)
    host.zone.status = {"step": "fail", "reason": "x"}
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and rm.sent(acts) == ["down"], f"taking навсегда: {svc.rsv_get('rsv_in')}"
    assert not svc.rsv_get("rsv_in").get("failing"), "отметка прежнего выпуска осталась в базе"
    assert {"teardown", "wipe"} <= set(host.zone.names())


def test_a_second_fail_while_the_first_runs_is_empty_and_the_mark_is_not_stored(host):
    svc = host.svc
    assert svc.reserve_go_req_in(7, "button") == ""
    seen = {}

    def _stop():
        seen["inner"] = svc._take_fail("второй")             # обработчик кода видит тот же fail
        seen["stored"] = svc.rsv_get("rsv_in").get("failing")
    svc._rsvopen_stop = _stop
    acts = svc._take_fail("первый")
    assert seen["inner"] == [] and not seen["stored"], seen
    assert rm.sent(acts) == ["down"] and svc.reserve_in() == "idle"
    assert "_rsv_take_failing" not in svc.__dict__, "отметка срыва осталась — следующий срыв не пройдёт"
