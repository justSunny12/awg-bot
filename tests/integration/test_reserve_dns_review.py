"""Запись DNS резерва после ревью (domain/services/reserve/dns.py, util/dnsapi):
проверка записи читает все адреса и TTL и предупреждает при второй A-записи и
TTL больше 300 с, посреди переезда не идёт; факт записи — ровно один адрес;
смена провайдера сбрасывает токен прежнего; DynDNS2 хранит сервер без пути и
терпит путь из записей прежних выпусков.

Мир DNS — DnsWorld из tests/integration/_reserve_move (авторитетный сервер,
системный резолвер, API провайдера).

Цена ошибки: вторая A-запись, принятая за «запись на резерве», — половина
клиентов идёт на мёртвую основу, а переезд «прошёл»; TTL 3600 без
предупреждения — клиенты переходят через час, админ ждёт пять минут; проверка
у DuckDNS/DynDNS2 посреди переезда — это запись, она вернула бы прежний адрес;
токен Cloudflare, отданный DuckDNS после смены провайдера, — секрет уходит не
тому; путь /nic/update дважды — каждое обновление DynDNS2 отвечает 404.
"""
from __future__ import annotations

import pytest

from awgbot.util.dnsapi import dyndns2

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP, PEER_IP, automatic

FOREIGN = "203.0.113.99"                 # чужой адрес второй A-записи

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


# ── проверка записи ─────────────────────────────────────────────────────────

def test_a_check_with_one_address_and_a_short_ttl_is_clean(mv):
    automatic(mv.svc)
    got = mv.svc.reserve_dns_check()
    assert got["ok"] is True and "⚠️" not in got["text"], got
    assert (got["addrs"], got["ttl"]) == ([OWN_IP], 60)
    d = mv.svc.rsv_get("rsv_dns")
    assert (d.get("addrs"), d.get("ttl"), d.get("check_ok")) == ([OWN_IP], 60, True), d


def test_a_second_a_record_is_a_warning(mv):
    automatic(mv.svc)
    mv.dns.auth = [OWN_IP, FOREIGN]
    got = mv.svc.reserve_dns_check()
    assert got["ok"] is False, "две A-записи прошли проверку"
    assert "оставь одну A-запись" in got["text"] and OWN_IP in got["text"] and FOREIGN in got["text"], got["text"]
    assert mv.svc.rsv_get("rsv_dns").get("addrs") == [OWN_IP, FOREIGN]


@pytest.mark.parametrize("ttl, warned", [(300, False), (301, True), (3600, True)])
def test_a_long_ttl_is_a_warning_with_the_minutes(mv, ttl, warned):
    automatic(mv.svc)
    mv.dns.ttl = ttl
    got = mv.svc.reserve_dns_check()
    assert got["ok"] is (not warned), got
    if warned:
        assert f"TTL {ttl} с" in got["text"] and f"{ttl // 60} мин" in got["text"], got["text"]


def test_the_check_waits_while_the_server_is_busy_moving(mv, monkeypatch):
    automatic(mv.svc)
    monkeypatch.setattr(mv.svc, "reserve_busy", lambda: "идёт переезд")
    reads = mv.dns.auth_reads
    got = mv.svc.reserve_dns_check()
    assert got["ok"] is False and "проверка подождёт" in got["text"], got
    assert mv.dns.auth_reads == reads and not mv.dns.sets, "проверка посреди переезда тронула запись"


def test_the_fact_of_the_record_is_exactly_one_address(mv):
    svc = mv.svc
    mv.dns.auth = [PEER_IP]
    assert svc.reserve_dns_points_to(PEER_IP) is True
    mv.dns.auth = [PEER_IP, FOREIGN]
    assert svc.reserve_dns_points_to(PEER_IP) is False
    mv.dns.auth = []
    assert svc.reserve_dns_points_to(PEER_IP) is False, "записи нет — это «не туда», а не «не прочиталось»"


# ── провайдер и токен ───────────────────────────────────────────────────────

def test_changing_the_provider_drops_the_token_of_the_previous_one(mv):
    svc = mv.svc
    svc.reserve_dns_set_provider("dyndns2")
    svc.reserve_dns_set_token("https://login:DUMMY-pass@dyn.example.org/nic/update")
    svc.reserve_dns_set_provider("duckdns")
    d = svc.rsv_get("rsv_dns")
    assert (d.get("token"), d.get("user"), d.get("server")) == ("", "", ""), f"секрет прежнего провайдера остался: {d}"


def test_choosing_the_same_provider_again_keeps_the_token(mv):
    svc = mv.svc
    svc.reserve_dns_set_provider("dyndns2")
    svc.reserve_dns_set_token("https://login:DUMMY-pass@dyn.example.org/nic/update")
    svc.reserve_dns_set_provider("dyndns2")
    d = svc.rsv_get("rsv_dns")
    assert (d.get("token"), d.get("user")) == ("DUMMY-pass", "login"), d


@pytest.mark.parametrize("server", ["https://dyn.example.org", "https://dyn.example.org/",
                                    "https://dyn.example.org/nic/update", "dyn.example.org",
                                    "dyn.example.org/nic/update/"])
def test_the_dyndns2_url_has_the_path_exactly_once(server):
    assert dyndns2._url({"server": server}) == "https://dyn.example.org/nic/update"


# ── надзор записи: записи нет, свой адрес среди нескольких ──────────────────

@pytest.fixture()
def watch(mv, monkeypatch):
    import types
    from awgbot.domain.services.reserve import dns as dnsmod
    monkeypatch.setattr(dnsmod, "random", types.SimpleNamespace(uniform=lambda a, b: 1.0))

    def tick():
        mv.clock.advance(dnsmod.RECORD_WATCH_SECONDS + 1)
        return mv.svc.reserve_record_watch_tick()
    mv.tick = tick
    return mv


def test_a_missing_record_is_told_once_loudly(watch):
    watch.dns.auth = []
    first = watch.tick()
    assert len(first) == 1 and first[0].force_sound, [n.text for n in first]
    assert "vpn-a.example.org" in first[0].text and not first[0].text.rstrip().endswith(".")
    assert watch.tick() == [], "одно и то же «записи нет» на каждом такте"


def test_my_address_among_others_is_told_once_per_set_of_addresses(watch):
    watch.dns.auth = [OWN_IP, FOREIGN]
    first = watch.tick()
    assert len(first) == 1 and FOREIGN in first[0].text and OWN_IP in first[0].text, [n.text for n in first]
    assert watch.tick() == []
    watch.dns.auth = [OWN_IP, FOREIGN, "203.0.113.98"]
    assert len(watch.tick()) == 1, "новый набор адресов — новое уведомление"


def test_the_record_back_on_me_resets_the_mark_and_a_new_loss_is_told_again(watch):
    watch.dns.auth = []
    assert len(watch.tick()) == 1
    watch.dns.auth = [OWN_IP]
    assert watch.tick() == [] and not watch.svc.rsv_get("rsv_record_watch").get("told")
    watch.dns.auth = []
    assert len(watch.tick()) == 1, "после возврата записи новая пропажа не сказана"


def test_the_watch_stays_silent_while_my_clients_are_away(watch):
    from tests.integration import _reserve_move as rm2
    rm2.go_away(watch.svc)
    watch.dns.auth = []
    assert watch.tick() == []
