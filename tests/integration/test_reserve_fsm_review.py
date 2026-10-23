"""Автоматы резерва после ревью (этап «автоматы и гонки»): срок из прежней
загрузки, Telegram в здоровье основы, возврат (перезапуск раз в минуту,
застрявший deleteWebhook, пустая и повторная реплика, повтор ошибки
слияния), сторона резерва (back при пустой зоне, back из taking до записи,
повтор up, срок после done, срыв один раз, relay уступает новому открытию).

Мир — фикстуры test_reserve_hosting (резерв) и test_reserve_return (основа):
зона, процессы, часы и DNS подставные.

Цена ошибки: срок из прежней загрузки, принятый за свежий, — шаг висит до
следующей перезагрузки; возврат без Telegram — бот немой обеим сторонам;
перезапуск на каждом такте — бот основы не успевает подняться; молчащий
застрявший deleteWebhook — админ не знает, что бот не отвечает; пустая
реплика, принятая за ошибку, — клиенты навсегда у резерва; одна реплика,
слитая дважды, — продления и трафик удвоены; повтор одной ошибки слияния
громко и с автоматическим возвратом — шум каждые пять минут; back при пустой
зоне без ответа — основа ждёт вечно; пересылка хвоста при сорванном до
записи переезде — клиентов на клоне не было, а DNAT уводит чужие пакеты;
срыв дважды — два «принять не удалось» и двойной teardown; relay, держащий
зону, — новый переезд невозможен неделю.
"""
from __future__ import annotations

import socket

import pytest

from awgbot.domain.services.reserve import inbound, outbound
from awgbot.infra import rsvzone
from awgbot.util import livestep, timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import notices, sent
from tests.integration.test_reserve_hosting import _taking, host, payload_of
from tests.integration.test_reserve_return import away, merge_world

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["away", "host", "merge_world"]

REAL_TG_REACHABLE = outbound.ReserveOutboundMixin._tg_reachable      # до подмены общей фикстурой


# ── срок из прежней загрузки ────────────────────────────────────────────────

@pytest.mark.parametrize("ahead, expired", [(60, False), (livestep.MONO_SANE_S - 1, False),
                                            (livestep.MONO_SANE_S + 1, True), (86400 * 7, True)])
def test_a_deadline_more_than_an_hour_ahead_is_from_a_previous_boot(ahead, expired):
    assert livestep.MONO_SANE_S == 3600
    assert livestep.mono_expired(1000.0 + ahead, 1000.0) is expired


# ── Telegram в здоровье основы ──────────────────────────────────────────────

def test_telegram_reachability_is_a_tcp_probe_cached_for_a_minute(away, monkeypatch):
    svc = away.svc
    calls = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def connect(addr, timeout=0):
        calls.append(addr)
        if state["down"]:
            raise OSError("нет сети")
        return _Conn()
    state = {"down": False}
    monkeypatch.setattr(socket, "create_connection", connect)
    assert REAL_TG_REACHABLE(svc) is True and calls == [("api.telegram.org", 443)]
    state["down"] = True
    assert REAL_TG_REACHABLE(svc) is True and len(calls) == 1, "проба чаще раза в минуту"
    away.clock.advance(61)
    assert REAL_TG_REACHABLE(svc) is False and len(calls) == 2


def test_without_telegram_the_primary_is_not_healthy(away, monkeypatch):
    svc = away.svc
    assert svc.reserve_healthy() is True
    monkeypatch.setattr(outbound.ReserveOutboundMixin, "_tg_reachable", lambda self: False)
    assert svc.reserve_healthy() is False, "возврат без Telegram — бот немой обеим сторонам"


# ── возврат: перезапуск и Telegram ──────────────────────────────────────────

def _at_step(w, step: str, **fields):
    w.svc.reserve_set_out("returning", kind="button", gen=7, return_started_at=timeutil.now_iso(), **fields)
    w.svc._out_step(step, 300)


def test_the_restart_is_asked_once_a_minute_not_on_every_tick(away):
    svc = away.svc
    _at_step(away, "restart")
    assert svc.reserve_out_tick() == [("restart",)]
    away.clock.advance(30)
    assert svc.reserve_out_tick() == [], "перезапуск на каждом такте — бот основы не успевает подняться"
    away.clock.advance(31)
    assert svc.reserve_out_tick() == [("restart",)]


def test_a_stuck_delete_webhook_is_told_once_on_the_hundredth_try(away):
    svc = away.svc
    _at_step(away, "take_tg")
    told = []
    for _ in range(150):
        acts = svc.reserve_out_tick()
        assert ("take_tg",) in acts
        told += notices(acts)
    assert len(told) == 1 and told[0].force_sound, f"застрявший deleteWebhook: {[n.text for n in told]}"
    assert "Beta" in told[0].text


# ── возврат: реплика ────────────────────────────────────────────────────────

def _returning(w):
    if w.svc.reserve_out() != "returning":                   # merge_world уже на шаге back_req
        w.svc.reserve_return_start("button")
    assert w.svc.rsv_get("rsv_out")["step"] == "back_req"


def test_an_empty_replica_returns_without_a_merge(merge_world):
    w = merge_world
    _returning(w)
    acts = w.svc.reserve_released_in(b"")
    assert acts == [("take_tg",), ("live",)], acts
    out = w.svc.rsv_get("rsv_out")
    assert out["step"] == "take_tg" and out["merge_report"] == {"lost": True}
    assert not [e for e in w.peers.events if e[0] in ("add", "remove")]


def test_the_same_replica_twice_is_merged_once(merge_world):
    w = merge_world
    _returning(w)
    blob = rm.snapshot_blob(w.svc)
    assert w.svc.reserve_released_in(blob) == [("restart",)]
    report = w.svc.rsv_get("rsv_out")["merge_report"]
    w.svc._out_step("back_req")                                # рестарт посреди: реплику попросили снова
    calls = []
    real = w.svc._return_merge
    w.svc._return_merge = lambda *a: calls.append(a) or real(*a)
    assert w.svc.reserve_released_in(blob) == [("restart",)]
    assert calls == [], "одна и та же реплика слита дважды — продления и трафик удвоены"
    assert w.svc.rsv_get("rsv_out")["merge_report"] == report


def test_the_same_merge_error_twice_is_silent_and_stops_the_automatic_return(merge_world):
    w = merge_world
    bad = rm.snapshot_blob(w.svc, k=b"\x02" * 32)
    _returning(w)
    first = notices(w.svc.reserve_released_in(bad))
    assert len(first) == 1 and first[0].force_sound and not first[0].silent
    assert not w.svc.rsv_get("rsv_out").get("merge_blocked")
    _returning(w)
    second = notices(w.svc.reserve_released_in(bad))
    out = w.svc.rsv_get("rsv_out")
    assert len(second) == 1 and second[0].silent and not second[0].force_sound, "повтор той же ошибки — громко"
    assert out["state"] == "away" and out.get("merge_blocked"), out
    w.svc.rsv_update("rsv_out", healthy_mono=1.0)
    w.clock.advance(outbound.HEALTHY_S + 60)
    assert ("send", "back_req", {}) not in w.svc.reserve_out_tick(), "автоматический возврат после двух одинаковых срывов"
    w.svc.reserve_return_start("button")                       # кнопка — остаётся
    assert w.svc.reserve_out() == "returning"


def test_a_successful_return_forgets_the_merge_marks(merge_world):
    w = merge_world
    w.svc.rsv_update("rsv_out", merge_error="x", merge_blocked="x", merged_fp="ab")
    _at_step(w, "relay")
    w.svc.reserve_wiped_in()
    out = w.svc.rsv_get("rsv_out")
    assert (out.get("merged_fp"), out.get("merge_error"), out.get("merge_blocked")) in (("", "", ""), (None, None, None)), out


# ── сторона резерва ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["idle", ""])
def test_back_with_an_empty_zone_answers_wiped(host, state):
    host.svc.reserve_set_in(state or "idle")
    assert host.svc.reserve_back_in() == [("send", "wiped", {})]
    assert host.zone.calls == []


@pytest.mark.parametrize("step, relays", [("launched", False), ("clone_up", False), ("record_set", True),
                                          ("done", True)])
def test_back_from_taking_relays_the_tail_only_if_clients_could_have_come(host, monkeypatch, step, relays):
    real = host.svc.reserve_peer_state
    monkeypatch.setattr(host.svc, "reserve_peer_state", lambda: {**real(), "self_host": "203.0.113.9", "ports": [45877]})
    _taking(host)
    host.zone.status = {"step": step}
    host.svc.reserve_back_in()
    names = host.zone.names()
    assert ("relay" in names) is relays, f"{step}: {host.zone.calls}"
    assert ("teardown" in names) is (not relays)
    assert bool(host.svc.rsv_get("rsv_in").get("relay_to")) is relays, host.svc.rsv_get("rsv_in")


def test_up_is_repeated_every_30_seconds_until_the_hosting_moves_on(host):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "clone_up"}
    assert sent(svc.reserve_in_tick()) == ["up"]
    host.clock.advance(10)
    assert "up" not in sent(svc.reserve_in_tick())
    host.clock.advance(25)
    assert sent(svc.reserve_in_tick()) == ["up"], "up не повторён — сессия оборвалась в секунду clone_up"


@pytest.mark.parametrize("active, why", [(False, "не поднялся"), (True, "не взял право отвечать")])
def test_an_emergency_hosting_that_did_not_take_telegram_fails_after_ten_minutes(host, monkeypatch, active, why):
    svc = host.svc
    monkeypatch.setattr(rsvzone, "guest_unit_active", lambda: active)
    _taking(host, "dead")
    host.zone.status = {"step": "done"}
    svc.reserve_in_tick()
    svc.reserve_in_tick()
    host.clock.advance(inbound.TAKEN_WAIT_S - 5)
    assert svc.reserve_in() == "taking" and "down" not in sent(svc.reserve_in_tick())
    host.clock.advance(10)
    acts = svc.reserve_in_tick()
    down = [a[2]["reason"] for a in acts if a[:2] == ("send", "down")]
    assert svc.reserve_in() == "idle" and down and why in down[0], acts


def test_a_failure_is_taken_once(host):
    svc = host.svc
    _taking(host)
    host.zone.status = {"step": "fail", "reason": "доли не сошлись"}
    first = svc._take_fail("доли не сошлись")
    assert first and host.zone.names().count("teardown") == 1
    assert svc._take_fail("доли не сошлись") == [] and svc.reserve_in_tick() == []
    assert host.zone.names().count("teardown") == 1, "срыв дважды — двойной teardown и два уведомления"


def test_the_tail_relay_yields_to_a_new_opening(host):
    svc = host.svc
    svc.reserve_set_in("relay", relay_to="203.0.113.9:45877", relay_until="2026-12-01T00:00:00+05:00")
    assert svc.reserve_zone_free() is True
    assert svc.reserve_go_req_in(7, "button") == "", "relay держит зону — новый переезд невозможен"
    assert svc.reserve_in() == "taking"
    assert host.zone.names()[:1] == ["teardown"], f"правило DNAT хвоста не снято до клона: {host.zone.calls}"
    assert not svc.rsv_get("rsv_in").get("relay_to")


@pytest.mark.parametrize("state, free", [("idle", True), ("relay", True), ("taking", False), ("serving", False),
                                         ("releasing", False)])
def test_the_zone_is_free_only_when_idle_or_relaying(host, state, free):
    host.svc.reserve_set_in(state)
    assert host.svc.reserve_zone_free() is free


# ── пересылка хвоста: тишина по счётчику правила DNAT ───────────────────────

def _relay(host, monkeypatch, *, since_h: float, pkts):
    from awgbot.core import settings
    from awgbot.infra import nftguard
    settings.set_value("app.reserve.relay_idle_hours", 2)
    box = {"pkts": pkts}
    monkeypatch.setattr(nftguard, "relay_packets", lambda: box["pkts"])
    host.svc.reserve_set_in("relay", relay_to="203.0.113.9:45877",
                            relay_at=timeutil.to_iso(timeutil.shift(timeutil.now(), -since_h * 3600)),
                            relay_until=timeutil.to_iso(timeutil.shift(timeutil.now(), 86400)))
    return box


def test_packets_through_the_tail_keep_the_relay_alive(host, monkeypatch):
    """Хвост ещё идёт (счётчик правила растёт) — пересылка живёт, хотя с её
    начала прошло больше часов тишины."""
    box = _relay(host, monkeypatch, since_h=0.1, pkts=10)
    host.svc.reserve_in_tick()                                 # первое чтение — отметка счётчика
    old = timeutil.to_iso(timeutil.shift(timeutil.now(), -3 * 3600))
    host.svc.rsv_update("rsv_in", relay_at=old, relay_last_traffic=old)   # прошло три часа
    box["pkts"] = 25                                          # клиенты, не сменившие запись, ещё шлют
    host.svc.reserve_in_tick()
    assert host.svc.reserve_in() == "relay", "пересылка снята, хотя пакеты через неё идут"
    assert host.svc.rsv_get("rsv_in").get("relay_pkts") == 25


def test_a_silent_tail_is_taken_down_after_the_quiet_hours(host, monkeypatch):
    box = _relay(host, monkeypatch, since_h=1, pkts=10)
    host.svc.reserve_in_tick()
    host.svc.rsv_update("rsv_in", relay_last_traffic=timeutil.to_iso(timeutil.shift(timeutil.now(), -2 * 3600 - 60)))
    acts = host.svc.reserve_in_tick()                          # счётчик не вырос
    assert host.svc.reserve_in() == "idle" and host.zone.names() == ["teardown"], host.zone.calls
    assert sent(acts) == ["st"]
    assert box["pkts"] == 10


def test_an_unreadable_counter_counts_the_silence_from_the_start_of_the_relay(host, monkeypatch):
    _relay(host, monkeypatch, since_h=1, pkts=None)
    host.svc.reserve_in_tick()
    assert host.svc.reserve_in() == "relay", "час с начала — меньше двух часов тишины"
    _relay(host, monkeypatch, since_h=3, pkts=None)
    host.svc.reserve_in_tick()
    assert host.svc.reserve_in() == "idle"


def test_the_relay_counter_is_read_from_the_dnat_rules_of_prerouting(monkeypatch):
    import json
    import subprocess as sp
    from awgbot.infra import nftguard
    doc = {"nftables": [{"metainfo": {}}, {"chain": {"name": "prerouting"}},
                        {"rule": {"expr": [{"match": {}}, {"counter": {"packets": 7, "bytes": 900}},
                                           {"dnat": {"addr": "203.0.113.9", "port": 45877}}]}},
                        {"rule": {"expr": [{"counter": {"packets": 1000, "bytes": 1}}, {"accept": None}]}},
                        {"rule": {"expr": [{"counter": {"packets": 5, "bytes": 1}}, {"dnat": {"addr": "203.0.113.9"}}]}}]}
    monkeypatch.setattr(nftguard, "_nft", lambda args, timeout=10, check=True:
                        sp.CompletedProcess(args, 0, json.dumps(doc).encode(), b""))
    assert nftguard.relay_packets() == 12, "в счёт пошли правила без dnat"
    monkeypatch.setattr(nftguard, "_nft", lambda args, timeout=10, check=True: sp.CompletedProcess(args, 1, b"", b"no chain"))
    assert nftguard.relay_packets() is None


# ── go с TTL записи ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("ttl, want", [(900, 900), (0, 0)])
def test_the_ttl_from_go_reaches_the_open_process(host, ttl, want):
    assert host.svc.reserve_go_req_in(7, "button", ttl=ttl) == ""
    payload = payload_of(host.spawn.popen[0])
    assert payload.get("dns") == {"ttl": want}, payload.get("dns")


# ── повтор ошибки слияния: кнопка возврата остаётся ─────────────────────────

def test_a_blocked_merge_keeps_the_return_button(merge_world):
    w = merge_world
    bad = rm.snapshot_blob(w.svc, k=b"\x02" * 32)
    for _ in range(2):
        _returning(w)
        w.svc.reserve_released_in(bad)
    assert w.svc.rsv_get("rsv_out").get("merge_blocked")
    w.svc.rsv_update("rsv_out", healthy_mono=1.0)
    w.clock.advance(outbound.HEALTHY_S + 60)
    acts = w.svc.reserve_out_tick()
    assert ("send", "back_req", {}) not in acts and w.svc.reserve_out() == "away"
    assert w.svc.rsv_get("rsv_out").get("can_return") is True, "кнопка «Вернуть клиентов» пропала"


# ── back: порядок действий — DNAT хвоста, затем сброс conntrack ─────────────

def test_back_redraws_the_tail_before_flushing_conntrack_and_then_says_relay(host, monkeypatch):
    real = host.svc.reserve_peer_state
    monkeypatch.setattr(host.svc, "reserve_peer_state", lambda: {**real(), "self_host": "203.0.113.9", "ports": [45877]})
    from tests.integration.test_reserve_hosting import _serving
    _serving(host)
    acts = host.svc.reserve_back_in()
    kinds = [a[0] if a[0] != "send" else f"send:{a[1]}" for a in acts]
    assert kinds[:3] == ["fw", "relay_flush", "send:relay"], \
        f"сброс conntrack не между перерисовкой DNAT и словом relay: {kinds}"


def test_back_without_the_address_of_the_primary_flushes_nothing(host, monkeypatch):
    real = host.svc.reserve_peer_state
    monkeypatch.setattr(host.svc, "reserve_peer_state", lambda: {**real(), "self_host": "", "ports": [45877]})
    monkeypatch.setattr(host.svc, "reserve_peer", lambda: {"host": ""})
    from tests.integration.test_reserve_hosting import _serving
    _serving(host)
    acts = host.svc.reserve_back_in()
    assert ("relay_flush",) not in acts and "teardown" in host.zone.names(), acts
