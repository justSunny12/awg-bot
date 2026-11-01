"""Отметка «вебхук гостя застрял» (`rsv_in.webhook_stalled`, guest:
reserve_guest_webhook_in) — об одном эпизоде: переход в taking (кнопкой
по `go` основы — inbound, по точкам или коду — opening) её сбрасывает
вместе с прочими полями прошлого эпизода.

Цена ошибки: отметка, пережившая эпизод, глушит Т-РУ19 следующего —
вебхук гостя нового эпизода застрял, а админ резерва узнаёт о нём только
по немому боту; а первое «не застрял» нового гостя приходит админу
ложным «вебхук снова принимает».
"""
from __future__ import annotations

import pytest

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_hosting import _serving, _taking, host

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]

STALLED = {"stalled": True, "since": "2026-11-01T10:00:00+05:00", "error": "Connection timed out"}


def _texts(acts) -> list[str]:
    return [n.text for a in acts if a[0] == "notify" for n in a[1]]


def _stalled_last_episode(host) -> None:
    _serving(host)
    assert len(_texts(host.svc.reserve_guest_webhook_in(STALLED))) == 1, "застой первого эпизода не сказан"
    assert host.svc.rsv_get("rsv_in")["webhook_stalled"] is True
    host.svc.reserve_set_in("idle", step="", reason="")        # эпизод кончился, отметка осталась


def test_a_new_episode_by_the_button_drops_the_stalled_mark_and_tells_again(host):
    _stalled_last_episode(host)
    _taking(host)
    assert not host.svc.rsv_get("rsv_in").get("webhook_stalled"), "отметка прошлого эпизода пережила taking"
    assert _texts(host.svc.reserve_guest_webhook_in({"stalled": False})) == [], \
        "первое «не застрял» нового гостя — ложное «снова принимает»"
    assert len(_texts(host.svc.reserve_guest_webhook_in(STALLED))) == 1, "застой нового эпизода не сказан"


def test_a_new_episode_by_points_drops_the_stalled_mark(host, monkeypatch):
    _stalled_last_episode(host)
    monkeypatch.setattr(host.svc, "reserve_auto_block", lambda: "")   # предусловия автопереезда — не здесь
    host.svc.reserve_open_by_points({"qpoints": [[1, "00" * 16], [2, "11" * 16]], "qbox": "00" * 56}, "dead", "q")
    assert host.svc.reserve_in() == "taking"
    assert not host.svc.rsv_get("rsv_in").get("webhook_stalled"), "отметка прошлого эпизода пережила taking"
