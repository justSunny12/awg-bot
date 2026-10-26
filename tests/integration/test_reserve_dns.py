"""Запись DNS адреса для пользователей (domain/services/reserve/dns.py): настройка
провайдера и токена, проверка, смена, факт у авторитетного сервера и надзор
за записью (Т-РУ16).

Провайдер и авторитетный сервер подставлены (`DnsWorld`): тест решает, что
отвечает чужой сервер зоны и что делали с API.

Цена ошибки: токен, не доехавший в конверт пакета, — аварийный переезд не
сможет сменить запись; токен в открытом виде вне состояния резерва — утечка;
надзор, который шлёт Т-РУ16 при каждом тике, — админ глохнет к настоящей
тревоге, а молчащий при чужом адресе — пользователи уходят на чужой сервер
незамеченными; «Вернуть запись» без сброса отметки — следующий уход записи
тоже промолчит.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve import dns as dnsmod
from awgbot.domain.services.types import ServiceError
from awgbot.util import dnsapi
from awgbot.util.dnsapi import common as dnscommon
from awgbot.util.rsvpkg import seal

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP, PEER_IP

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
FOREIGN = "203.0.113.77"


# ── настройка ───────────────────────────────────────────────────────────────

def test_the_provider_is_manual_until_chosen_and_an_unknown_one_is_refused(mv):
    svc = mv.svc
    assert svc.reserve_dns_provider() == "manual" and not svc.reserve_dns_automatic()
    with pytest.raises(ServiceError):
        svc.reserve_dns_set_provider("nope")
    assert svc.reserve_dns_provider() == "manual"


def test_a_provider_without_a_token_is_still_manual_for_the_move(mv):
    svc = mv.svc
    svc.reserve_dns_set_provider("cloudflare")
    assert svc.reserve_dns_provider() == "cloudflare" and not svc.reserve_dns_automatic(), \
        "без токена бот не может править запись — переезд обязан ждать человека"
    svc.reserve_dns_set_token("DUMMY-token")
    assert svc.reserve_dns_automatic()


@pytest.mark.parametrize("bad", ["", "   ", "с пробелом внутри", "x" * 513])
def test_a_token_that_does_not_look_like_a_token_is_refused_and_stores_nothing(mv, bad):
    mv.svc.reserve_dns_set_provider("duckdns")
    with pytest.raises(ServiceError):
        mv.svc.reserve_dns_set_token(bad)
    assert not mv.svc.reserve_dns_automatic()


def test_a_new_provider_drops_the_old_check_result(mv):
    svc = mv.svc
    svc.reserve_dns_set_provider("cloudflare")
    svc.reserve_dns_set_token("DUMMY-token")
    svc.reserve_dns_check()
    assert svc.reserve_dns_screen_data()["check_ok"] is True
    svc.reserve_dns_set_provider("duckdns")
    d = svc.reserve_dns_screen_data()
    assert d["check_ok"] is None and d["checked_at"] == "", "результат проверки прежнего провайдера показан как свежий"


def test_dyndns2_splits_the_url_into_user_secret_and_server(mv):
    svc = mv.svc
    svc.reserve_dns_set_provider("dyndns2")
    svc.reserve_dns_set_token("https://login:DUMMY-pass@dyn.example.org:8443/nic/update")
    d = svc.rsv_get("rsv_dns")
    assert (d["user"], d["token"], d["server"]) == ("login", "DUMMY-pass", "https://dyn.example.org:8443"), \
        "сервер хранится без пути — путь /nic/update добавляет провайдер"


@pytest.mark.parametrize("bad", ["DUMMY-token", "http://l:p@dyn.example.org/nic/update", "https://dyn.example.org/nic/update"])
def test_dyndns2_refuses_everything_but_https_with_a_login(mv, bad):
    mv.svc.reserve_dns_set_provider("dyndns2")
    with pytest.raises(ServiceError):
        mv.svc.reserve_dns_set_token(bad)
    assert not mv.svc.reserve_dns_automatic()


def test_forgetting_the_token_returns_the_move_to_the_manual_mode(mv):
    svc = mv.svc
    rm.automatic(svc)
    svc.reserve_dns_forget_token()
    assert not svc.reserve_dns_automatic() and svc.reserve_dns_provider() == "cloudflare"
    assert svc.rsv_get("rsv_dns").get("token") == ""


def test_the_token_travels_to_the_reserve_only_inside_the_dns_envelope_under_m(mv):
    """Токен нужен резерву для аварийной смены записи; открыть конверт можно только долей `M`,
    сама часть данных под `K` его не несёт."""
    svc = mv.svc
    built = svc.reserve_pkg_build(force=True)
    assert not seal.header(bytes.fromhex(built["blob"])).has_dns, "провайдер не настроен — конверта нет"
    svc.reserve_dns_set_provider("cloudflare")
    svc.reserve_dns_set_token("DUMMY-token")
    built = svc.reserve_pkg_build(force=True)
    blob = bytes.fromhex(built["blob"])
    assert seal.header(blob).has_dns
    m = bytes.fromhex(svc.reserve_keys()["m"])
    env = seal.open_dns(blob, m)
    assert env["token"] == "DUMMY-token" and env["provider"] == "cloudflare"
    parts = seal.open_parts(blob, svc.reserve_k())
    assert "DUMMY-token" not in str(parts), "токен попал в часть под K — доступен тому, у кого только K"


def test_the_envelope_names_the_record_the_reserve_will_change(mv):
    """Процесс открытия на резерве меняет запись по `dns["name"]` и ждёт факт по нему же; конверт
    без имени — провайдеру не на что указать, ожидание факта идёт по пустому имени."""
    svc = mv.svc
    rm.automatic(svc)
    blob = bytes.fromhex(svc.reserve_pkg_build(force=True)["blob"])
    env = seal.open_dns(blob, bytes.fromhex(svc.reserve_keys()["m"]))
    assert env.get("name") == rc.HOST_A, env


# ── проверка ────────────────────────────────────────────────────────────────

def test_the_check_of_an_ip_address_says_there_is_no_record(mv):
    settings.set_value("app.network.server_host", "203.0.113.5")
    res = mv.svc.reserve_dns_check()
    assert res["ok"] is False and "IP" in res["text"]


def test_the_manual_check_reads_the_authoritative_server(mv):
    res = mv.svc.reserve_dns_check()
    assert res["ok"] is True and res["ip"] == OWN_IP and rc.HOST_A in res["text"]
    assert mv.svc.reserve_dns_screen_data()["current"] == OWN_IP


def test_the_manual_check_with_an_unreadable_zone_fails_with_the_reason(mv):
    mv.dns.auth_error = True
    res = mv.svc.reserve_dns_check()
    assert res["ok"] is False and "не прочитана" in res["text"] and "не ответил" in res["text"]
    assert mv.svc.reserve_dns_screen_data()["check_ok"] is False


def test_the_automatic_check_tells_about_rights_when_the_provider_refuses(mv):
    svc = mv.svc
    rm.automatic(svc)
    mv.dns.check_error = "токен без права править зону"
    res = svc.reserve_dns_check()
    assert res["ok"] is False and "не даёт править" in res["text"] and "без права" in res["text"]
    mv.dns.check_error = ""
    mv.dns.api_value = "203.0.113.9"
    res = svc.reserve_dns_check()
    assert res["ok"] is True and res["ip"] == "203.0.113.9" and "доступна для правки" in res["text"]


# ── смена и факт ────────────────────────────────────────────────────────────

def test_the_manual_provider_refuses_the_switch_with_a_human_reason(mv, monkeypatch):
    monkeypatch.setattr(dnsapi, "provider_for", dnscommon.provider_for)
    with pytest.raises(ServiceError, match="вручную"):
        mv.svc.reserve_dns_switch(PEER_IP)


def test_a_provider_error_becomes_a_service_error_and_a_success_is_remembered(mv):
    svc = mv.svc
    rm.automatic(svc)
    mv.dns.set_error = "HTTP 403"
    with pytest.raises(ServiceError, match="403"):
        svc.reserve_dns_switch(PEER_IP)
    assert not svc.rsv_get("rsv_dns").get("switched_to")
    mv.dns.set_error = ""
    svc.reserve_dns_switch(PEER_IP)
    assert svc.rsv_get("rsv_dns")["switched_to"] == PEER_IP and mv.dns.sets == [PEER_IP]


def test_the_switch_of_an_ip_address_is_refused(mv):
    settings.set_value("app.network.server_host", "203.0.113.5")
    with pytest.raises(ServiceError):
        mv.svc.reserve_dns_switch(PEER_IP)


def test_the_fact_is_what_the_authoritative_server_says_not_what_the_api_accepted(mv):
    svc = mv.svc
    assert svc.reserve_dns_points_to(PEER_IP) is False
    mv.dns.auth = [PEER_IP, FOREIGN]
    assert svc.reserve_dns_points_to(PEER_IP) is False, "вторая A-запись увела бы половину пользователей мимо"
    mv.dns.auth = [PEER_IP]
    assert svc.reserve_dns_points_to(PEER_IP) is True
    assert svc.rsv_get("rsv_dns").get("addrs") == [PEER_IP] and svc.rsv_get("rsv_dns").get("ttl") == 60
    mv.dns.auth_error = True
    assert svc.reserve_dns_points_to(PEER_IP) is None, "не прочиталось — это не «нет»"
    assert svc.reserve_dns_points_to("") is None


def test_own_ip_is_resolved_once_per_ten_minutes_and_an_ip_host_needs_no_resolver(mv):
    svc = mv.svc
    assert svc.reserve_own_ip() == OWN_IP and svc.reserve_own_ip() == OWN_IP
    assert mv.dns.system_reads == 1, "кэш не работает — резолвер дёргается на каждый экран"
    mv.clock.advance(601)
    svc.reserve_own_ip()
    assert mv.dns.system_reads == 2
    settings.set_value("app.network.self_host", "203.0.113.50")
    assert svc.reserve_own_ip() == "203.0.113.50" and mv.dns.system_reads == 2


def test_the_peer_ip_comes_from_its_st_and_falls_back_to_the_resolver_for_a_name(mv):
    svc = mv.svc
    assert svc.reserve_peer_ip() == PEER_IP
    rm.st_in(svc, self="beta.example.net")
    mv.dns.system["beta.example.net"] = ["203.0.113.8"]
    assert svc.reserve_peer_ip() == "203.0.113.8"


# ── надзор за записью ───────────────────────────────────────────────────────

@pytest.fixture()
def watch(mv, monkeypatch):
    monkeypatch.setattr(dnsmod, "random", types.SimpleNamespace(uniform=lambda a, b: 1.0))
    mv.clock.advance(1000)
    return mv


def _tick(w):
    w.clock.advance(dnsmod.RECORD_WATCH_SECONDS + 1)
    return w.svc.reserve_record_watch_tick()


def test_the_watch_is_silent_while_the_record_points_here(watch):
    assert _tick(watch) == []


def test_a_foreign_address_is_announced_once_with_sound_and_a_return_button(watch):
    watch.dns.auth = [FOREIGN]
    notes = _tick(watch)
    assert len(notes) == 1
    n = notes[0]
    assert n.tg_id == config.ADMIN_ID and n.force_sound is True and n.action == ("rsv_record_back", 0)
    assert FOREIGN in n.text and rc.HOST_A in n.text and "Переезда не было" in n.text
    assert _tick(watch) == [], "тот же чужой адрес — второе уведомление: админ глохнет к тревоге"
    watch.dns.auth = ["203.0.113.78"]
    assert len(_tick(watch)) == 1, "запись ушла на другой чужой адрес — это новое событие"


def test_after_the_record_comes_back_the_next_departure_is_announced_again(watch):
    watch.dns.auth = [FOREIGN]
    _tick(watch)
    watch.dns.auth = [OWN_IP]
    assert _tick(watch) == []
    watch.dns.auth = [FOREIGN]
    assert len(_tick(watch)) == 1


def test_the_watch_does_not_poll_the_authoritative_server_more_often_than_its_period(watch):
    watch.dns.auth = [FOREIGN]
    _tick(watch)
    reads = watch.dns.auth_reads
    watch.clock.advance(30)
    assert watch.svc.reserve_record_watch_tick() == [] and watch.dns.auth_reads == reads


@pytest.mark.parametrize("state", ["leaving", "away", "returning"])
def test_the_watch_is_silent_while_the_clients_are_at_the_reserve(watch, state):
    """Запись указывает на резерв — так и должно быть, пока пользователи там."""
    watch.dns.auth = [PEER_IP]
    watch.svc.reserve_set_out(state)
    assert _tick(watch) == []


def test_the_watch_survives_an_unreadable_zone_and_an_unknown_own_address(watch):
    watch.dns.auth_error = True
    assert _tick(watch) == []
    watch.dns.auth_error = False
    watch.dns.auth = [FOREIGN]
    watch.dns.system = {}
    watch.clock.advance(700)                                      # кэш своего адреса истёк
    watch.svc.__dict__.pop("_rsv_own_ip", None)
    assert _tick(watch) == [], "свой адрес не определился — сравнивать не с чем, тревога была бы ложной"


def test_the_watch_without_a_pair_or_with_an_ip_host_does_nothing(services, rsv, mv):
    settings.set_value("app.network.server_host", "203.0.113.5")
    mv.clock.advance(1000)
    assert mv.svc.reserve_record_watch_tick() == []


def test_returning_the_record_switches_it_to_the_own_address_and_rearms_the_watch(watch):
    svc = watch.svc
    rm.automatic(svc)
    watch.dns.auth = [FOREIGN]
    _tick(watch)
    assert svc.reserve_record_return() == ""
    assert watch.dns.sets == [OWN_IP]
    watch.dns.auth = ["203.0.113.78"]
    assert len(_tick(watch)) == 1, "после возврата записи отметка «уже сказал» должна быть снята"


def test_returning_the_record_reports_why_it_failed(watch):
    svc = watch.svc
    rm.automatic(svc)
    watch.dns.set_error = "HTTP 500"
    assert "500" in svc.reserve_record_return()
    watch.dns.set_error = ""
    watch.dns.system = {}
    svc.__dict__.pop("_rsv_own_ip", None)
    assert "не определился" in svc.reserve_record_return()
