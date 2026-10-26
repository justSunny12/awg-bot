"""Сеть резерва после ревью — части с базой: порт стороны зоны мимо портов
клонов второй стороны (zonelinks `_free_port`), адрес основы для DNAT хвоста
— IP (inbound `_relay_host_ip`), маршрут запасного слота гостя через адрес
хоста в /30 (rfzone `_slot_route_via`), внеплановое обновление пула Telegram
при serving и при кандидате вне анонсов (tgpool `reserve_tg_pool_refresh_soon`).

Цена ошибки: сторона зоны на порту клона — при переезде клон второй стороны
не встаёт (порт занят у хоста), пользователи без VPN; имя в правиле DNAT — nft
резолвит его один раз при загрузке, а при отказе резолва таблица не грузится
вовсе (файервол хоста резерва без правил); маршрут через veth без via — зонд
запасного слота мёртв, РФ-доступ пользователей основы у резерва считается
сломанным; пул Telegram из старого кэша — вебхук с нового адреса Telegram не
доходит до бота основы в зоне весь переезд; внеплановое обновление по
каждому сканеру — запрос к источнику анонсов каждые 10 минут.
"""
from __future__ import annotations

import socket
import time
import types

import pytest

from awgbot.domain.services.reserve import inbound, rfzone, tgpool, zonelinks
from awgbot.infra import nftguard

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_hosting import _serving, _taking, host
from tests.integration.test_reserve_tgpool import learn, pool

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host", "learn", "pool"]


# ── порт стороны зоны ───────────────────────────────────────────────────────

@pytest.fixture()
def ports(services, monkeypatch):
    monkeypatch.setattr(zonelinks, "PORT_LO", 45000)
    monkeypatch.setattr(zonelinks, "PORT_HI", 45004)          # кандидаты 45000..45003
    monkeypatch.setattr(nftguard, "listen_ports", lambda: [45003])
    return services


def test_the_zone_side_port_avoids_the_clone_ports_of_the_other_side(ports):
    ports.rsv_put("rsv_peer_state", {"ports": [45000, 45001]})
    got = {ports._free_port(set()) for _ in range(50)}
    assert got == {45002}, f"сторона зоны на порту клона второй стороны или на слушаемом: {got}"


def test_with_every_port_busy_there_is_a_clear_refusal(ports):
    ports.rsv_put("rsv_peer_state", {"ports": [45000, 45001, 45002]})
    with pytest.raises(RuntimeError, match="свободный порт"):
        ports._free_port(set())


def test_garbage_in_the_peer_ports_is_ignored(ports):
    ports.rsv_put("rsv_peer_state", {"ports": ["45000", None, 45001]})
    assert {ports._free_port(set()) for _ in range(50)} == {45000, 45002}


# ── адрес основы для хвоста ─────────────────────────────────────────────────

@pytest.mark.parametrize("host_in, want", [("203.0.113.9", "203.0.113.9"), ("", "")])
def test_an_ip_is_taken_as_it_is(host_in, want, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("IP-литерал резолвится"))
    assert inbound.ReserveInboundMixin._relay_host_ip(host_in) == want


def test_a_name_is_resolved_to_its_first_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 2, 17, "", ("203.0.113.9", 0)),
                                                                (2, 2, 17, "", ("203.0.113.10", 0))])
    assert inbound.ReserveInboundMixin._relay_host_ip("vpn.example.net") == "203.0.113.9"


def test_a_name_that_does_not_resolve_gives_nothing(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror(-2, "Name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert inbound.ReserveInboundMixin._relay_host_ip("vpn.example.net") == ""


def _named_primary(host, monkeypatch, *, resolves: bool):
    real = host.svc.reserve_peer_state
    monkeypatch.setattr(host.svc, "reserve_peer_state",
                        lambda: {**real(), "self_host": "vpn.example.net", "ports": [45877]})

    def gai(name, *a, **k):
        if not resolves:
            raise socket.gaierror(-2, "Name or service not known")
        return [(2, 2, 17, "", ("203.0.113.9", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", gai)


def test_the_tail_goes_to_the_address_of_the_primary_not_to_its_name(host, monkeypatch):
    _serving(host)
    _named_primary(host, monkeypatch, resolves=True)
    host.svc.reserve_back_in()
    relays = [c for c in host.zone.calls if c[0] == "relay"]
    assert relays == [("relay", "203.0.113.9:45877")], f"в DNAT хвоста не адрес: {host.zone.calls}"


def test_a_primary_name_that_does_not_resolve_tears_the_zone_down_instead(host, monkeypatch):
    _serving(host)
    _named_primary(host, monkeypatch, resolves=False)
    host.svc.reserve_back_in()
    assert "relay" not in host.zone.names() and "teardown" in host.zone.names(), host.zone.calls


# ── маршрут запасного слота гостя ───────────────────────────────────────────

@pytest.mark.parametrize("spare, want", [
    (types.SimpleNamespace(link_cidr="169.254.77.4/30"), "169.254.77.5"),
    (types.SimpleNamespace(link_cidr=""), ""),
    (types.SimpleNamespace(link_cidr="мусор"), ""),
    (None, ""),
])
def test_the_spare_slot_route_goes_via_the_first_host_of_its_cidr(services, monkeypatch, spare, want):
    monkeypatch.setattr(services, "guest_rf_spare", lambda: spare)
    assert services._slot_route_via(rfzone.RF_SPARE_ID) == want


def test_a_real_slot_route_has_no_via(services, monkeypatch):
    monkeypatch.setattr(services, "guest_rf_spare", lambda: types.SimpleNamespace(link_cidr="169.254.77.4/30"))
    assert services._slot_route_via(1) == "" and services._slot_route_via(2) == ""


# ── пул Telegram: внеплановое обновление ────────────────────────────────────

def test_serving_asks_for_fresh_announces_before_the_first_webhook(host):
    svc = host.svc
    _taking(host)
    svc.__dict__["_tg_pool_next"] = time.monotonic() + 86400       # плановое — через сутки
    host.zone.status = {"step": "done"}
    svc.reserve_in_tick()
    nxt = svc.__dict__["_tg_pool_next"] - time.monotonic()
    assert 0 < nxt <= 8, f"анонсы не обновятся до вебхука: следующее через {nxt:.0f} с"


def test_a_candidate_outside_every_announce_asks_for_fresh_announces_once_per_half_hour(learn, monkeypatch):
    clock = types.SimpleNamespace(t=10_000.0)
    monkeypatch.setattr(tgpool, "time", types.SimpleNamespace(monotonic=lambda: clock.t))
    learn._tg_announce({"62041": ["149.154.160.0/20"]})
    learn.__dict__["_tg_pool_next"] = clock.t + 86400
    learn.seen.data = {"198.51.100.10": 100}                       # вне анонсов
    learn.reserve_tg_learn_tick()
    first = learn.__dict__["_tg_pool_next"]
    assert 30 <= first - clock.t <= 90, f"кандидат вне кэша не позвал обновление: {first - clock.t:.0f} с"
    clock.t += 600
    learn.__dict__["_tg_pool_next"] = clock.t + 86400
    learn.reserve_tg_learn_tick()
    assert learn.__dict__["_tg_pool_next"] == clock.t + 86400, "обновление по каждому сканеру — чаще раза в 30 минут"
    clock.t += 1300
    learn.reserve_tg_learn_tick()
    assert learn.__dict__["_tg_pool_next"] - clock.t <= 90, "через полчаса кандидат вне кэша снова не зовёт обновление"


def test_refresh_soon_without_a_reason_is_not_damped(services, monkeypatch):
    clock = types.SimpleNamespace(t=500.0)
    monkeypatch.setattr(tgpool, "time", types.SimpleNamespace(monotonic=lambda: clock.t))
    services.reserve_tg_pool_refresh_soon(5)
    services.__dict__["_tg_pool_next"] = 1e9
    services.reserve_tg_pool_refresh_soon(5)
    assert services.__dict__["_tg_pool_next"] <= clock.t + 7, "переход в serving не обновил анонсы второй раз"
