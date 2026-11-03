"""Досев набора при добавлении домена «➕ Сайт»: бот после реконсиляции
спрашивает dnsmasq бота о каждом новом домене, адреса в набор кладёт сам
dnsmasq по строке ipset= — бот в набор ничего не пишет.

Свой DNS-клиент (dnsapi.common.query) и адрес резолвера (resolver.listen_addrs)
подменены; часы досева — тоже: общий предел 10 с на весь досев (правило
«наружу ≤ 10 с») проверяется без ожидания.
"""
import types

import pytest

from awgbot.domain.services.routing import access as access_mod
from awgbot.infra import resolver, routing
from awgbot.infra.routing.feeds import ask_dnsmasq as _real_ask
from awgbot.util.dnsapi import common as dns

pytestmark = pytest.mark.integration

_RESOLVER = "10.8.1.1"


def _a(name, ip="203.0.113.7"):
    return (name, 1, bytes(int(x) for x in ip.split(".")), b"", 0, 60)


class _Dnsmasq:
    """dnsmasq бота за dnsapi.common.query: ответы по домену, молчание — по
    таймауту запроса на подменённых часах. Набор НЕ трогает: так видно, что
    бот сам в набор ничего не дописывает."""

    def __init__(self, answers=None, silent=()):
        self.answers = dict(answers or {})
        self.silent = set(silent)
        self.t = 0.0
        self.calls = []            # (адрес, домен, тип, таймаут, время начала)
        self.events = []           # ("start"|"end", домен) — запросы не перекрываются
        self.busy = False

    def monotonic(self):
        return self.t

    def query(self, server, name, qtype, timeout=dns.TIMEOUT):
        assert not self.busy, "досев спрашивает параллельно"
        self.busy = True
        self.events.append(("start", name))
        self.calls.append((server, name, qtype, timeout, self.t))
        try:
            if name in self.silent or name not in self.answers:
                self.t += timeout
                raise dns.DnsError(f"сервер DNS {server} не ответил: timed out")
            self.t += 0.05
            return self.answers[name]
        finally:
            self.events.append(("end", name))
            self.busy = False


@pytest.fixture()
def dnsmasq(monkeypatch, fake_routing):
    """Настоящий ask_dnsmasq (фикстура fake_routing подменяет его моделью) поверх
    подменённого DNS-клиента, резолвер бота на _RESOLVER, часы досева — свои."""
    fake = _Dnsmasq()
    monkeypatch.setattr(routing.feeds, "ask_dnsmasq", _real_ask)
    monkeypatch.setattr(dns, "query", fake.query)
    monkeypatch.setattr(resolver, "listen_addrs", lambda path=None: [_RESOLVER, "10.8.2.1"])
    monkeypatch.setattr(access_mod, "time", types.SimpleNamespace(monotonic=fake.monotonic))
    return fake


def test_asks_the_bot_resolver_for_each_new_domain_in_order(
        services, make_active_client, fake_routing, dnsmasq):
    """По одному A-запросу на новый домен — первому адресу резолвера бота, по
    порядку и без перекрытия: следующий — после ответа на предыдущий."""
    dnsmasq.answers = {d: [_a(d)] for d in ("a.com", "b.com", "c.com")}
    c = make_active_client()
    res = services.routing_add_domains(c.id, "a.com b.com c.com")

    assert [(s, n, q) for s, n, q, _t, _at in dnsmasq.calls] == [
        (_RESOLVER, "a.com", 1), (_RESOLVER, "b.com", 1), (_RESOLVER, "c.com", 1)]
    assert dnsmasq.events == [(e, d) for d in ("a.com", "b.com", "c.com") for e in ("start", "end")]
    assert (res.routed_now, res.routed_later) == (3, 0)


def test_preseed_asks_after_the_resolver_got_the_new_lines(
        services, make_active_client, fake_routing, dnsmasq, monkeypatch):
    """Спрашивать имеет смысл только dnsmasq с новой строкой ipset=: запрос
    к нему — после записи конфига реконсиляцией."""
    seen = []

    def query(server, name, qtype, timeout=dns.TIMEOUT):
        seen.append(fake_routing.conf or "")
        return [_a(name)]

    monkeypatch.setattr(dns, "query", query)
    c = make_active_client()
    services.routing_add_domains(c.id, "bank.com")
    assert seen and f"ipset=/bank.com/{routing.user_set(c.id)}" in seen[0]


def test_the_bot_does_not_write_into_the_set(services, make_active_client, fake_routing, dnsmasq):
    """Адреса в набор кладёт dnsmasq по своей строке ipset=, как от запроса
    устройства, — бот ничего не дописывает: ответ есть, а набор, который
    «dnsmasq» в этой подмене не наполняет, пуст."""
    dnsmasq.answers = {"bank.com": [_a("bank.com", "203.0.113.7"), _a("bank.com", "203.0.113.8")]}
    c = make_active_client()
    res = services.routing_add_domains(c.id, "bank.com")
    assert res.routed_now == 1
    assert fake_routing.sets[routing.user_set(c.id)] == []
    assert not hasattr(routing.feeds, "add_networks"), "дописывать в набор боту нечем и незачем"


def test_silent_resolver_keeps_the_whole_preseed_within_10_seconds(
        services, make_active_client, fake_routing, dnsmasq):
    """Молчащий dnsmasq: каждый запрос — до своего таймаута, но весь досев не
    дольше 10 с. Домен, на котором предел исчерпан, и все следующие не
    спрашиваются и идут «при первом запросе»."""
    doms = [f"s{i}.example" for i in range(8)]
    c = make_active_client()
    res = services.routing_add_domains(c.id, " ".join(doms))

    timeouts = [t for _s, _n, _q, t, _at in dnsmasq.calls]
    assert timeouts == [3.0, 3.0, 3.0, 1.0], timeouts
    assert all(at + t <= 10.0 for _s, _n, _q, t, at in dnsmasq.calls)
    assert dnsmasq.t <= 10.0
    assert [n for _s, n, *_ in dnsmasq.calls] == doms[:4]
    assert (res.routed_now, res.routed_later) == (0, 8)
    assert res.added == doms, "досев не срывает добавление"


def test_answers_before_the_limit_count_as_routed_now(
        services, make_active_client, fake_routing, dnsmasq):
    """Ответившие до исчерпания предела направлены сразу, молчащие и
    неспрошенные — при первом запросе."""
    dnsmasq.answers = {"a.com": [_a("a.com")], "d.com": [_a("d.com")], "e.com": [_a("e.com")]}
    dnsmasq.silent = {"b.com", "c.com", "x.com"}
    c = make_active_client()
    res = services.routing_add_domains(c.id, "a.com b.com c.com d.com x.com e.com")
    # a — 0,05 с; b, c — по 3 с; d — 0,05; x — 3 с (до 9,1); e — 0,05 (до 9,15)
    assert [n for _s, n, *_ in dnsmasq.calls] == ["a.com", "b.com", "c.com", "d.com", "x.com", "e.com"]
    assert (res.routed_now, res.routed_later) == (3, 3)


def test_answer_without_addresses_or_an_error_is_not_routed_now():
    """NXDOMAIN (пустой ответ), ответ без A-записей и ошибка сервера — не
    «направлен сразу»; ошибка — не исключение."""
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(dns, "query", lambda s, n, q, timeout=3.0: [])
        assert _real_ask(_RESOLVER, "none.example", 1.0) is False
        mp.setattr(dns, "query", lambda s, n, q, timeout=3.0: [("x.example", 5, b"", b"", 0, 60)])
        assert _real_ask(_RESOLVER, "x.example", 1.0) is False

        def boom(s, n, q, timeout=3.0):
            raise dns.DnsError("сервер DNS 10.8.1.1: код ошибки 2")
        mp.setattr(dns, "query", boom)
        assert _real_ask(_RESOLVER, "bad.example", 1.0) is False
        mp.setattr(dns, "query", lambda s, n, q, timeout=3.0: [_a("ok.example")])
        assert _real_ask(_RESOLVER, "ok.example", 1.0) is True
    finally:
        mp.undo()


def test_no_bot_resolver_on_the_host_means_no_preseed(
        services, make_active_client, fake_routing, dnsmasq, monkeypatch):
    """Резолвера бота на хосте нет — спрашивать некого: досева нет, все
    добавленные — при первом запросе, добавление не срывается."""
    monkeypatch.setattr(resolver, "listen_addrs", lambda path=None: [])
    dnsmasq.answers = {"bank.com": [_a("bank.com")]}
    c = make_active_client()
    res = services.routing_add_domains(c.id, "bank.com")
    assert dnsmasq.calls == []
    assert res.added == ["bank.com"]
    assert (res.routed_now, res.routed_later) == (0, 1)


def test_failed_reconcile_means_no_preseed(
        services, make_active_client, fake_routing, dnsmasq, monkeypatch):
    """Реконсиляция не применилась — строки ipset= в dnsmasq нет, и ответ на
    запрос в набор не ляжет: досева нет, «направлено сразу» не обещаем."""
    def refuse(text, path=None):
        raise routing.RoutingError("dnsmasq отверг конфиг списков")
    monkeypatch.setattr(routing.feeds, "write_dnsmasq_conf", refuse)
    dnsmasq.answers = {"bank.com": [_a("bank.com")]}
    c = make_active_client()
    res = services.routing_add_domains(c.id, "bank.com")
    assert dnsmasq.calls == []
    assert (res.routed_now, res.routed_later) == (0, 1)


def test_resolver_answer_lands_in_the_profile_set(services, make_active_client, fake_routing):
    """Сквозной путь на модели dnsmasq фикстуры: запрос бота — и адреса в наборе
    профиля по строке ipset=, как от запроса устройства."""
    fake_routing.dns["bank.com"] = ["203.0.113.7", "203.0.113.8"]
    mp = pytest.MonkeyPatch()
    mp.setattr(resolver, "listen_addrs", lambda path=None: [_RESOLVER])
    try:
        c = make_active_client()
        res = services.routing_add_domains(c.id, "bank.com nothing.example")
    finally:
        mp.undo()
    assert [d for _s, d, _t in fake_routing.asked] == ["bank.com", "nothing.example"]
    members = fake_routing.sets[routing.user_set(c.id)]
    assert "203.0.113.7" in members and "203.0.113.8" in members
    assert (res.routed_now, res.routed_later) == (1, 1)
