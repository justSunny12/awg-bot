"""Щель между переходом в taking и запуском процесса открытия (opening
`reserve_open_by_points`, inbound `_go_launch`, openproc `_rsvopen_forget`,
`_rsvopen_exit`): состояние taking пишется под `_LOCK`, а `_rsvopen_launch`
(хранилище гостя, systemd-run) идёт уже после замка — такт `_tick_taking`
из другого потока может прийти раньше, чем новый процесс ляжет в память.
Процесс прошлого эпизода (кнопкой: дошёл до done и вышел, пока зона
обслуживала, — его кода никто не спрашивал) к этому моменту уже забыт.

Щель воспроизводится подменой монтирования хранилища гостя: оно — первый
шаг `_rsvopen_launch`, и такт, прогнанный изнутри него, видит ровно то,
что видел бы такт другого потока после замка, но до запуска.

Цена ошибки: такт читает код 0 прошлого процесса и засчитывает новому
открытию срыв «процесс открытия завершился без итога (код 0)» — основе
`down`, зона снята, шлюзам «переезд не состоялся», а новый процесс в это
время доходит до done зря; аварийный переезд при мёртвой основе срывается
на ровном месте и ждёт паузы после отказа.
"""
from __future__ import annotations

import time
import types

import pytest

from awgbot.domain.services.reserve import opening
from awgbot.infra import rsvzone

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import sent
from tests.integration.test_reserve_hosting import _serving, _taking, host

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]

POINTS = {"qpoints": [[1, "00" * 16], [2, "11" * 16]], "qbox": "00" * 56}


def _last_episode_by_the_button_exited(host) -> None:
    """Прошлый эпизод — кнопкой: процесс дошёл до done и вышел с кодом 0, зона обслуживала
    (такт serving код не спрашивает), пользователи вернулись — состояние idle."""
    _taking(host)
    host.zone.status = {"step": "done"}
    host.spawn.rc = 0
    _serving(host)
    host.svc.reserve_in_tick()
    host.svc.reserve_set_in("idle", step="", reason="")
    assert host.svc.__dict__.get("_rsvopen_proc") is not None, "процесс прошлого эпизода уже забыт — щели нет"


def _tick_in_the_gap(host, monkeypatch) -> list:
    """Такт «другого потока» — в щели после замка, до запуска нового процесса; новый процесс
    после этого идёт (код None)."""
    seen: list = []
    real_store = rsvzone.store

    def store(size_mb: int = 0) -> str:
        if not seen:
            assert host.svc.reserve_in() == "taking", "щель не та: taking ещё не записан"
            seen.append(host.svc.reserve_in_tick())
            host.spawn.rc = None
        return real_store(size_mb)
    monkeypatch.setattr(rsvzone, "store", store)
    return seen


def _no_failure(host, acts) -> None:
    assert host.svc.reserve_in() == "taking", \
        f"новое открытие сорвано по коду прошлого процесса: {host.svc.rsv_get('rsv_in').get('reason')}"
    assert "down" not in sent(acts), acts
    assert "teardown" not in host.zone.names(), "зона снята по коду прошлого процесса"


def test_a_tick_in_the_gap_before_a_button_launch_does_not_read_the_last_exit(host, monkeypatch):
    _last_episode_by_the_button_exited(host)
    seen = _tick_in_the_gap(host, monkeypatch)
    assert host.svc.reserve_go_req_in(7, "button") == ""
    assert seen, "такт в щели не прошёл"
    _no_failure(host, seen[0])
    assert len(host.spawn.popen) == 2 and host.svc.reserve_in_tick() == []


def test_a_tick_in_the_gap_before_an_open_by_points_does_not_read_the_last_exit(host, monkeypatch):
    _last_episode_by_the_button_exited(host)
    monkeypatch.setattr(host.svc, "reserve_auto_block", lambda: "")   # предусловия автопереезда — не здесь
    monkeypatch.setattr(opening, "time", types.SimpleNamespace(monotonic=host.clock.monotonic, time=time.time))  # срок — по часам теста
    seen = _tick_in_the_gap(host, monkeypatch)
    host.svc.reserve_open_by_points(dict(POINTS), "dead", "q")
    assert seen, "такт в щели не прошёл"
    _no_failure(host, seen[0])
    assert host.svc.reserve_in_tick() == [] and host.svc.reserve_in() == "taking"


def test_the_new_process_exit_is_still_seen_after_the_launch(host, monkeypatch):
    """Забыт только прошлый процесс: свой, вышедший без итога, такт по-прежнему ловит сразу."""
    _last_episode_by_the_button_exited(host)
    _tick_in_the_gap(host, monkeypatch)
    assert host.svc.reserve_go_req_in(7, "button") == ""
    host.spawn.rc = 137
    acts = host.svc.reserve_in_tick()
    assert host.svc.reserve_in() == "idle" and "down" in sent(acts)
    assert host.svc.rsv_get("rsv_in").get("reason") == "процесс открытия завершился без итога (код 137)"
