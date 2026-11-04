"""`back` основы при живом госте с журналом (inbound_back: reserve_back_in,
_back_after_released; journal/handover: reserve_guest_released_jrn).

Срыв переезда у основы после передачи бота (резерв был недоступен): её `back` ушёл в
мёртвый канал и повторяется с дома (outbound_return, test_reserve_lost_back), а гость у
резерва жив, держит эпоху и писал за время разрыва — эти правки основа ждёт хвостом
(отметка С9). Снять зону по `back` сразу значило бы убить гостя с неотправленными
записями и стереть его журнал (`--wipe`):

- `back` в taking | serving при госте, назвавшем свою эпоху (hello/online), — гостю `stop`
  (как на back_req), releasing с отметкой `back_pending`; зона не трогается;
- повтор `back`, пока гость отдаёт записи, — ждать;
- его `released` — снятие зоны тем же путём, что обычный back (relay | teardown, wipe,
  `relay`, `wiped`); `released` основе не идёт — она дома, не в возврате;
- гость молчит REPLICA_WAIT_S — released без реплики и то же снятие;
- гостя с журналом нет (прежний путь, гость не сказал о себе) — `back` снимает зону сразу.

Окружение — `host` из test_reserve_hosting (зона и процессы подставные).

Цена ошибки: правки людей на резерве за время разрыва теряются молча; или наоборот —
резерв держит зону и гостя до «не ждать?» у основы, отвергая каждый её пакет («зона занята»).
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.reserve import inbound

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_hosting import PEER_IP, _serving, _taking, host, sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
host = host


def _guest_alive(host, e: int = 1) -> None:
    """Гость на локальном канале назвал свою эпоху (hello/online)."""
    host.svc.reserve_guest_jrn_seen({"e": e, "fork": 0, "last": 3})


def _released(host) -> list[tuple]:
    return host.svc.reserve_guest_released_jrn({"line": host.svc.reserve_keep_line(), "epoch": 1, "last": 3})


def test_back_at_a_live_journal_guest_stops_it_first_and_leaves_the_zone(host):
    svc = host.svc
    _serving(host)
    _guest_alive(host)
    acts = svc.reserve_back_in()
    assert acts == [("guest", "stop", {})], acts
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "releasing" and inn.get("back_pending") is True and not inn.get("released_at")
    assert host.zone.calls == [], "зона снята до остановки гостя — его последние записи потеряны"
    assert svc.reserve_back_in() == [], "повтор back при госте, отдающем записи, не ждёт его released"
    assert host.zone.calls == [] and svc.reserve_in() == "releasing"


def test_the_guests_released_completes_the_back_without_a_released_to_the_primary(host):
    svc = host.svc
    _serving(host)
    _guest_alive(host)
    svc.reserve_back_in()
    acts = _released(host)
    assert not [a for a in acts if a[0] == "send_released"], "released основе, которая дома после срыва, — лишний"
    assert sent(acts) == ["relay", "wiped"], acts
    assert host.zone.calls == [("relay", f"{PEER_IP}:45871"), ("wipe",)], "снятие зоны не тем путём, что обычный back"
    assert svc.reserve_in() == "relay" and not svc.rsv_get("rsv_in").get("back_pending")
    assert not svc.rsv_get("rsv_pkg_in"), "прежний пакет основы пережил back"


def test_back_while_opening_stops_the_guest_and_the_released_tears_the_zone_down(host):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "done"}
    _guest_alive(host)
    assert svc.reserve_back_in() == [("guest", "stop", {})]
    assert svc.reserve_in() == "releasing"
    acts = _released(host)
    assert sent(acts) == ["relay", "wiped"] and svc.reserve_in() == "relay", acts


def test_a_silent_guest_releases_by_the_deadline_and_the_back_completes(host):
    svc = host.svc
    _serving(host)
    _guest_alive(host)
    svc.reserve_back_in()
    host.clock.advance(inbound.REPLICA_WAIT_S - 1)
    assert svc.reserve_in_tick() == [] and svc.reserve_in() == "releasing"
    host.clock.advance(2)
    acts = svc.reserve_in_tick()
    assert sent(acts) == ["relay", "wiped"], acts
    assert svc.reserve_in() == "relay" and host.zone.names()[-1] == "wipe"


def test_back_without_a_journal_guest_tears_the_zone_down_at_once(host):
    svc = host.svc
    _serving(host)
    acts = svc.reserve_back_in()
    assert sent(acts) == ["relay", "wiped"] and svc.reserve_in() == "relay", acts
    assert host.zone.names()[-1] == "wipe"
