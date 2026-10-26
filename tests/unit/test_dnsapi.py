"""A-запись адреса для пользователей: провайдеры DNS и чтение записи у
авторитетного сервера (util/dnsapi/).

Запись — арбитр VPN: куда она указывает, туда идут пользователи. Меняет её основа
или резерв, а факт смены проверяется чтением — у провайдера и у
авторитетного сервера зоны своим маленьким UDP-клиентом. Сеть здесь
подменена целиком: HTTP — заглушкой `http` в модуле провайдера, DNS —
подставным `socket.socket`, который отвечает синтетическими сообщениями (со
сжатием имён указателями, как отвечают настоящие серверы).

Цена ошибки: успех API, принятый при отказе, — бот уверен, что пользователи
переехали, а запись прежняя; отказ, принятый за успех у DuckDNS/DynDNS2 (они
отвечают 200 с текстом), — то же; разбор ответа DNS, который падает
необработанным исключением, — проверка факта смены роняет задачу вместо
строки «не удалось проверить»; чужой ответ (не наш id), принятый за свой, —
подложенный адрес считается фактом.
"""
from __future__ import annotations

import base64
import json
import socket
import struct
import urllib.error

import pytest

from awgbot.util import dnsapi
from awgbot.util.dnsapi import cloudflare, common, digitalplat, duckdns, dyndns2, manual

pytestmark = pytest.mark.unit


# ── синтетические сообщения DNS ──────────────────────────────────────────────

def _name(n: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in n.strip(".").split(".")) + b"\x00"


def _answer(query: bytes, records: list[tuple], *, rcode: int = 0, qid: int | None = None,
            authority: list[tuple] = ()) -> bytes:
    """Ответ на запрос `query`: вопрос копируется, имя каждой записи —
    указатель на имя вопроса (0xC00C); rdata — байты как есть."""
    rid = struct.unpack(">H", query[:2])[0] if qid is None else qid
    question = query[12:]
    out = struct.pack(">HHHHHH", rid, 0x8180 | rcode, 1, len(records), len(authority), 0) + question
    for rtype, rdata, *ttl in list(records) + list(authority):     # (тип, rdata[, ttl]); ttl по умолчанию 300
        out += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, ttl[0] if ttl else 300, len(rdata)) + rdata
    return out


def _qname_of(query: bytes) -> tuple[str, int]:
    off, labels = 12, []
    while query[off]:
        ln = query[off]
        labels.append(query[off + 1:off + 1 + ln].decode())
        off += 1 + ln
    qtype = struct.unpack(">H", query[off + 1:off + 3])[0]
    return ".".join(labels), qtype


class _Dns:
    """Сеть DNS: системный резолвер 192.0.2.53 знает NS зоны example.net,
    авторитетный сервер (ns1.example.net = 203.0.113.53) знает A-записи.
    `reply` — как отвечать на (сервер, имя, тип); по умолчанию — по таблицам."""

    def __init__(self, monkeypatch):
        self.a = {"vpn.example.net": ["198.51.100.7"]}
        self.ns = {"example.net": "ns1.example.net"}
        self.ns_addr = {"ns1.example.net": ["203.0.113.53"]}
        self.sent: list[tuple[str, str, int]] = []
        self.override = None
        monkeypatch.setattr(common, "_system_resolvers", lambda: ["192.0.2.53"])
        monkeypatch.setattr(common.socket, "socket", self._socket)
        monkeypatch.setattr(common.socket, "getaddrinfo", self._gai)

    def _gai(self, host, port, family=0, type=0, *a, **kw):
        addrs = self.ns_addr.get(host) or self.a.get(host)
        if not addrs:
            raise socket.gaierror("нет такого имени")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in addrs]

    def respond(self, server: str, query: bytes) -> bytes | None:
        name, qtype = _qname_of(query)
        self.sent.append((server, name, qtype))
        if self.override:
            got = self.override(server, name, qtype, query)
            if got is not None:
                return got
        if server == "192.0.2.53" and qtype == 2:
            if name in self.ns:
                # rdata NS — имя со сжатием: «ns1» + указатель на имя зоны в вопросе
                return _answer(query, [(2, b"\x03ns1\xc0\x0c")])
            return _answer(query, [])                         # NOERROR без NS — укорачиваем имя
        if server in ("203.0.113.53",) and qtype == 1:
            if name not in self.a:
                return _answer(query, [], rcode=3)            # NXDOMAIN
            return _answer(query, [(1, socket.inet_aton(ip)) for ip in self.a[name]])
        return None

    def _socket(self, *a, **kw):
        dns = self

        class S:
            def __init__(self):
                self.reply = None

            def settimeout(self, t):
                pass

            def sendto(self, msg, addr):
                self.reply = dns.respond(addr[0], msg)

            def recvfrom(self, n):
                if self.reply is None:
                    raise socket.timeout("timed out")
                return self.reply, ("x", 53)

            def close(self):
                pass
        return S()


@pytest.fixture()
def dns(monkeypatch):
    return _Dns(monkeypatch)


def test_the_authoritative_server_is_found_by_shortening_the_name_and_asked_directly(dns):
    assert dnsapi.read_authoritative("vpn.example.net") == ["198.51.100.7"]
    asked = [(s, n, t) for s, n, t in dns.sent]
    assert ("192.0.2.53", "vpn.example.net", 2) in asked and ("192.0.2.53", "example.net", 2) in asked
    assert asked[-1] == ("203.0.113.53", "vpn.example.net", 1), "A-запрос ушёл не авторитетному серверу"


def test_several_addresses_come_back_sorted_and_without_repeats(dns):
    dns.a["vpn.example.net"] = ["203.0.113.9", "198.51.100.7", "203.0.113.9"]
    assert dnsapi.read_authoritative("vpn.example.net") == ["198.51.100.7", "203.0.113.9"]


def test_nxdomain_means_no_record_not_an_error(dns):
    assert dnsapi.read_authoritative("nothing.example.net") == []


def test_an_answer_with_someone_elses_id_is_not_ours(dns):
    def forged(server, name, qtype, query):
        if server == "203.0.113.53":
            rid = (struct.unpack(">H", query[:2])[0] + 1) % 65536
            return _answer(query, [(1, socket.inet_aton("203.0.113.66"))], qid=rid)
    dns.override = forged
    with pytest.raises(dnsapi.DnsError):
        dnsapi.read_authoritative("vpn.example.net")


def test_a_server_failure_code_is_an_error(dns):
    dns.override = lambda s, n, t, q: _answer(q, [], rcode=2) if s == "203.0.113.53" else None
    with pytest.raises(dnsapi.DnsError):
        dnsapi.read_authoritative("vpn.example.net")


def test_silence_of_the_authoritative_server_is_an_error(dns):
    real = dns.respond

    def silent(server, query):
        return None if server == "203.0.113.53" else real(server, query)
    dns.respond = silent
    with pytest.raises(dnsapi.DnsError):
        dnsapi.read_authoritative("vpn.example.net")


def test_no_ns_anywhere_is_an_error(dns):
    dns.ns.clear()
    with pytest.raises(dnsapi.DnsError, match="авторитетные"):
        dnsapi.read_authoritative("vpn.example.net")


def test_without_a_system_resolver_reading_is_an_error(dns, monkeypatch):
    monkeypatch.setattr(common, "_system_resolvers", lambda: [])
    with pytest.raises(dnsapi.DnsError):
        dnsapi.read_authoritative("vpn.example.net")


@pytest.mark.parametrize("cut", [13, 20, 40])
def test_a_truncated_answer_is_a_dns_error_not_a_crash(dns, cut):
    """Ответ оборван (MTU, порча): вызывающий ловит DnsError и пишет «не
    удалось проверить»; IndexError/struct.error уронил бы задачу проверки."""
    def short(server, name, qtype, query):
        if server == "203.0.113.53":
            return _answer(query, [(1, socket.inet_aton("198.51.100.7"))])[:cut]
    dns.override = short
    with pytest.raises(dnsapi.DnsError):
        dnsapi.read_authoritative("vpn.example.net")


def test_a_query_carries_the_name_and_type_asked(dns):
    recs = common.query("203.0.113.53", "vpn.example.net", 1)
    assert [(t, rdata) for _n, t, rdata, _m, _o, _ttl in recs] == [(1, socket.inet_aton("198.51.100.7"))]
    assert recs[0][0] == "vpn.example.net", "имя-указатель записи не развернулось"


@pytest.mark.parametrize("bad", ["a..b", "", "x" * 64 + ".example.net"])
def test_a_bad_name_is_refused_before_the_network(bad):
    """Имя записи вводит человек: пустая метка или метка длиннее 63 —
    DnsError с понятным текстом, а не исключение кодека."""
    with pytest.raises(dnsapi.DnsError):
        common._qname(bad)


def test_the_system_resolver_view_and_its_failure(monkeypatch):
    monkeypatch.setattr(common.socket, "getaddrinfo",
                        lambda *a, **kw: [(2, 1, 6, "", ("203.0.113.9", 0)), (2, 1, 6, "", ("198.51.100.7", 0)),
                                          (2, 1, 6, "", ("203.0.113.9", 0))])
    assert dnsapi.read_system("vpn.example.net") == ["198.51.100.7", "203.0.113.9"]

    def fail(*a, **kw):
        raise socket.gaierror("нет")
    monkeypatch.setattr(common.socket, "getaddrinfo", fail)
    assert dnsapi.read_system("vpn.example.net") == []


def test_ipv4_accepts_only_an_ipv4_address():
    assert common.ipv4(" 198.51.100.7\n") == "198.51.100.7"
    for bad in ("", "2001:db8::1", "198.51.100.300", "vpn.example.net", None):
        with pytest.raises(dnsapi.DnsError):
            common.ipv4(bad)


# ── HTTP ─────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body.encode()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_returns_the_code_and_body_even_for_an_http_error(monkeypatch):
    seen = {}

    def urlopen(req, timeout):
        seen["req"] = req
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, _Resp(403, "nope"))
    monkeypatch.setattr(common.net4, "urlopen", urlopen)
    assert common.http("GET", "https://dns.example.net/x", auth=("user", "DUMMY-pass")) == (403, "nope")
    hdr = seen["req"].get_header("Authorization")
    assert hdr == "Basic " + base64.b64encode(b"user:DUMMY-pass").decode()


def test_http_without_an_answer_is_a_dns_error(monkeypatch):
    def urlopen(req, timeout):
        raise urllib.error.URLError("timed out")
    monkeypatch.setattr(common.net4, "urlopen", urlopen)
    with pytest.raises(dnsapi.DnsError, match="не ответил"):
        common.http("GET", "https://dns.example.net/x")


def test_http_sends_a_json_body(monkeypatch):
    seen = {}

    def urlopen(req, timeout):
        seen["req"] = req
        return _Resp(200, "{}")
    monkeypatch.setattr(common.net4, "urlopen", urlopen)
    assert common.http("PATCH", "https://dns.example.net/x", body={"a": 1}) == (200, "{}")
    assert json.loads(seen["req"].data) == {"a": 1} and seen["req"].get_method() == "PATCH"
    assert seen["req"].get_header("Content-type") == "application/json"


# ── провайдеры ───────────────────────────────────────────────────────────────

class _Http:
    """Заглушка `http` провайдера: маршруты (метод, начало пути) → (код, тело)."""

    def __init__(self, monkeypatch, module, routes):
        self.routes = routes
        self.calls: list[dict] = []
        monkeypatch.setattr(module, "http", self)

    def __call__(self, method, url, *, headers=None, body=None, timeout=None, auth=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, "body": body, "auth": auth})
        for (m, prefix), resp in self.routes.items():
            if m == method and prefix in url:
                return resp(url) if callable(resp) else resp
        return 404, "{}"


CF = {"provider": "cloudflare", "name": "vpn.example.net", "token": "DUMMY-cf"}


def _cf_routes(**over):
    routes = {
        ("GET", "/zones?name=vpn.example.net&"): (200, json.dumps({"success": True, "result": []})),
        ("GET", "/zones?name=example.net&"): (200, json.dumps({"success": True, "result": [{"id": "Z1"}]})),
        ("GET", "/zones/Z1/dns_records?type=A&name=vpn.example.net"):
            (200, json.dumps({"success": True, "result": [{"id": "R1", "content": "198.51.100.7", "ttl": 120}]})),
        ("PATCH", "/zones/Z1/dns_records/R1"): (200, json.dumps({"success": True, "result": {}})),
    }
    routes.update(over)
    return routes


def test_cloudflare_finds_the_zone_by_shortening_the_name_and_reads_the_record(monkeypatch):
    h = _Http(monkeypatch, cloudflare, _cf_routes())
    assert cloudflare.read(CF) == "198.51.100.7"
    assert all(c["headers"].get("Authorization") == "Bearer DUMMY-cf" for c in h.calls)


def test_cloudflare_set_patches_the_record_with_its_ttl_and_without_proxy(monkeypatch):
    """`proxied: false` обязательно: через прокси Cloudflare UDP пользователей не
    пройдёт, VPN у всех ляжет при «успешной» смене."""
    h = _Http(monkeypatch, cloudflare, _cf_routes())
    cloudflare.set(CF, "203.0.113.9")
    patch = [c for c in h.calls if c["method"] == "PATCH"]
    assert len(patch) == 1 and patch[0]["url"].endswith("/zones/Z1/dns_records/R1")
    assert patch[0]["body"] == {"content": "203.0.113.9", "ttl": 120, "proxied": False}


def test_cloudflare_set_refuses_a_non_ipv4_before_the_patch(monkeypatch):
    h = _Http(monkeypatch, cloudflare, _cf_routes())
    with pytest.raises(dnsapi.DnsError):
        cloudflare.set(CF, "vpn.example.net")
    assert not [c for c in h.calls if c["method"] == "PATCH"]


@pytest.mark.parametrize("over, words", [
    ({("PATCH", "/zones/Z1/dns_records/R1"): (403, json.dumps({"success": False, "errors": [
        {"code": 10000, "message": "Authentication error"}]}))}, "Authentication error"),
    ({("PATCH", "/zones/Z1/dns_records/R1"): (200, json.dumps({"success": False, "errors": []}))}, "код 200"),
    ({("PATCH", "/zones/Z1/dns_records/R1"): (502, "<html>Bad gateway</html>")}, "не JSON"),
    ({("GET", "/zones?name=example.net&"): (200, json.dumps({"success": True, "result": []}))}, "зона"),
    ({("GET", "/zones/Z1/dns_records?type=A&name=vpn.example.net"):
        (200, json.dumps({"success": True, "result": []}))}, "A-записи"),
])
def test_cloudflare_api_failures_are_dns_errors(monkeypatch, over, words):
    _Http(monkeypatch, cloudflare, _cf_routes(**{}) | over)
    with pytest.raises(dnsapi.DnsError, match=words):
        cloudflare.set(CF, "203.0.113.9")


DUCK = {"provider": "duckdns", "name": "myvpn.duckdns.org", "token": "DUMMY-duck"}


def test_duckdns_ok_is_success_and_anything_else_is_a_refusal(monkeypatch):
    h = _Http(monkeypatch, duckdns, {("GET", "duckdns.org/update"): (200, "OK")})
    duckdns.set(DUCK, "203.0.113.9")
    url = h.calls[0]["url"]
    assert "domains=myvpn&" in url and "ip=203.0.113.9" in url and "token=DUMMY-duck" in url
    for status, text in ((200, "KO"), (200, ""), (500, "OK")):
        _Http(monkeypatch, duckdns, {("GET", "duckdns.org/update"): (status, text)})
        with pytest.raises(dnsapi.DnsError):
            duckdns.set(DUCK, "203.0.113.9")


def test_duckdns_refuses_a_name_outside_duckdns_without_calling_it(monkeypatch):
    h = _Http(monkeypatch, duckdns, {("GET", "duckdns.org/update"): (200, "OK")})
    with pytest.raises(dnsapi.DnsError, match="duckdns.org"):
        duckdns.set({**DUCK, "name": "vpn.example.net"}, "203.0.113.9")
    assert not h.calls


def test_duckdns_reads_through_the_authoritative_server(monkeypatch):
    monkeypatch.setattr(duckdns, "read_authoritative", lambda name: ["198.51.100.7"] if name == DUCK["name"] else [])
    assert duckdns.read(DUCK) == "198.51.100.7"
    monkeypatch.setattr(duckdns, "read_authoritative", lambda name: [])
    with pytest.raises(dnsapi.DnsError):
        duckdns.read(DUCK)


DYN = {"provider": "dyndns2", "name": "vpn.example.net", "server": "dynupdate.example.net", "user": "me",
       "token": "DUMMY-dyn"}


@pytest.mark.parametrize("answer", ["good 203.0.113.9", "nochg 203.0.113.9", "good"])
def test_dyndns2_good_and_nochg_are_success(monkeypatch, answer):
    h = _Http(monkeypatch, dyndns2, {("GET", "/nic/update"): (200, answer)})
    dyndns2.set(DYN, "203.0.113.9")
    c = h.calls[0]
    assert c["url"] == "https://dynupdate.example.net/nic/update?hostname=vpn.example.net&myip=203.0.113.9", \
        "сервер без схемы — https"
    assert c["auth"] == ("me", "DUMMY-dyn"), "DynDNS2 — basic auth"


@pytest.mark.parametrize("status, answer", [(200, "badauth"), (200, "nohost"), (200, "911"), (200, ""),
                                            (401, "good"), (200, "goodbye")])
def test_dyndns2_other_answers_are_refusals(monkeypatch, status, answer):
    _Http(monkeypatch, dyndns2, {("GET", "/nic/update"): (status, answer)})
    with pytest.raises(dnsapi.DnsError):
        dyndns2.set(DYN, "203.0.113.9")


def test_dyndns2_keeps_an_explicit_scheme_and_refuses_an_empty_server(monkeypatch):
    h = _Http(monkeypatch, dyndns2, {("GET", "/nic/update"): (200, "good")})
    dyndns2.set({**DYN, "server": "http://dyn.example.net/"}, "203.0.113.9")
    assert h.calls[0]["url"].startswith("http://dyn.example.net/nic/update?")
    with pytest.raises(dnsapi.DnsError, match="сервер"):
        dyndns2.set({**DYN, "server": " "}, "203.0.113.9")


def test_manual_cannot_set_and_reads_through_the_authoritative_server(monkeypatch):
    with pytest.raises(dnsapi.NotSupported):
        manual.set({"provider": "manual", "name": "vpn.example.net"}, "203.0.113.9")
    assert issubclass(dnsapi.NotSupported, dnsapi.DnsError)
    monkeypatch.setattr(manual, "read_authoritative", lambda name: ["198.51.100.7"])
    assert manual.check({"provider": "manual", "name": "vpn.example.net"}) == "198.51.100.7"


def test_the_provider_is_chosen_by_id_and_an_unknown_one_is_refused():
    assert dnsapi.provider_for({"provider": " Cloudflare "}) is cloudflare
    assert dnsapi.provider_for({"provider": "duckdns"}) is duckdns
    assert dnsapi.provider_for({"provider": "dyndns2"}) is dyndns2
    assert dnsapi.provider_for({"provider": "manual"}) is manual
    for cfg in ({"provider": "route53"}, {}, None):
        with pytest.raises(dnsapi.DnsError):
            dnsapi.provider_for(cfg)


def test_the_ttl_of_the_record_is_the_smallest_of_the_answer(dns):
    """По TTL бот предупреждает админа (больше 300 — пользователи переедут поздно)
    и ждёт перед «переподключи VPN»: берётся наименьший из A-записей ответа."""
    def answer(server, name, qtype, query):
        if server == "203.0.113.53" and qtype == 1:
            return _answer(query, [(1, socket.inet_aton("198.51.100.7"), 3600), (1, socket.inet_aton("203.0.113.9"), 120)])
    dns.override = answer
    assert dnsapi.read_authoritative_ttl("vpn.example.net") == (["198.51.100.7", "203.0.113.9"], 120)
    recs = common.query("203.0.113.53", "vpn.example.net", 1)
    assert sorted(r[5] for r in recs) == [120, 3600], "TTL записи не разобран"


def test_no_record_has_ttl_zero(dns):
    assert dnsapi.read_authoritative_ttl("nothing.example.net") == ([], 0)


DP = {"provider": "digitalplat", "name": "vpn.example.dpdns.org", "token": "DUMMY-dp_live"}
_DP_DOMAINS = json.dumps({"success": True, "data": [
    {"domain": "dpdns.org", "status": "active"},            # короткий суффикс — ловушка
    {"domain": "example.dpdns.org", "status": "active"},    # нужный, самый длинный
    {"domain": "other.dpdns.org", "status": "active"},
]})


def _dp_records(*recs):
    return json.dumps({"success": True, "data": list(recs)})


_DP_A = {"id": "R7", "type": "A", "name": "vpn", "value": "198.51.100.7", "ttl": 120, "protected": False,
         "etag": "W/\"e1\""}


def _dp_routes(over=None):
    # Порядок важен: заглушка матчит по вхождению начала пути, и «/domains»
    # вошёл бы и в путь записей — записи первыми.
    routes = {
        ("GET", "/domains/example.dpdns.org/dns/records"): (200, _dp_records(_DP_A)),
        ("GET", "/domains/dpdns.org/dns/records"): (200, _dp_records(
            {"id": "X1", "type": "A", "name": "vpn.example", "value": "203.0.113.200", "ttl": 60})),
        ("GET", "/domains"): (200, _DP_DOMAINS),
        ("PATCH", "/domains/example.dpdns.org/dns/records/R7"):
            (202, json.dumps({"success": True, "data": {"operation_id": "op1"}})),
    }
    routes.update(over or {})
    return routes


def test_digitalplat_picks_the_longest_domain_and_reads_the_record(monkeypatch):
    """Домен — самый длинный суффикс имени: взяв `dpdns.org` вместо
    `example.dpdns.org`, бот читал бы чужую зону и менял бы не ту запись."""
    h = _Http(monkeypatch, digitalplat, _dp_routes())
    assert digitalplat.read(DP) == "198.51.100.7"
    urls = [c["url"] for c in h.calls]
    assert any(u.endswith("/domains/example.dpdns.org/dns/records") for u in urls), urls
    assert not any("/domains/dpdns.org/" in u for u in urls), "зона короткого суффикса не должна читаться"
    assert all(c["headers"].get("Authorization") == "Bearer DUMMY-dp_live" for c in h.calls)


def test_digitalplat_reads_the_domain_itself_as_the_at_record(monkeypatch):
    cfg = dict(DP, name="example.dpdns.org")
    rec = dict(_DP_A, name="@", value="203.0.113.4")
    _Http(monkeypatch, digitalplat, _dp_routes({("GET", "/domains/example.dpdns.org/dns/records"):
                                                  (200, _dp_records(rec))}))
    assert digitalplat.read(cfg) == "203.0.113.4"
    assert digitalplat.check(cfg) == "203.0.113.4", "check — это чтение через API"


def test_digitalplat_set_patches_by_id_with_ttl_etag_and_a_fresh_idempotency_key(monkeypatch):
    """Без `If-Match` правка сядет поверх чужой; с одинаковым `Idempotency-Key`
    второй переезд провайдер посчитает повтором первого и адрес не сменит."""
    h = _Http(monkeypatch, digitalplat, _dp_routes())
    digitalplat.set(DP, "203.0.113.9")
    digitalplat.set(DP, "203.0.113.10")
    patch = [c for c in h.calls if c["method"] == "PATCH"]
    assert len(patch) == 2 and all(c["url"].endswith("/domains/example.dpdns.org/dns/records/R7") for c in patch)
    assert patch[0]["body"] == {"value": "203.0.113.9", "ttl": 120, "disabled": False}
    assert patch[1]["body"]["value"] == "203.0.113.10"
    assert all(c["headers"].get("If-Match") == "W/\"e1\"" for c in patch)
    keys = [c["headers"].get("Idempotency-Key") for c in patch]
    assert all(keys) and keys[0] != keys[1], f"ключ идемпотентности пуст или повторяется: {keys}"


def test_digitalplat_set_without_an_etag_sends_no_if_match_and_defaults_the_ttl(monkeypatch):
    rec = {"id": "R7", "type": "A", "name": "vpn", "value": "198.51.100.7"}
    h = _Http(monkeypatch, digitalplat, _dp_routes({("GET", "/domains/example.dpdns.org/dns/records"):
                                                      (200, _dp_records(rec))}))
    digitalplat.set(DP, "203.0.113.9")
    patch = [c for c in h.calls if c["method"] == "PATCH"]
    assert len(patch) == 1 and "If-Match" not in patch[0]["headers"]
    assert patch[0]["body"]["ttl"] == 300 and patch[0]["headers"].get("Idempotency-Key")


def test_digitalplat_set_refuses_a_non_ipv4_before_the_patch(monkeypatch):
    h = _Http(monkeypatch, digitalplat, _dp_routes())
    with pytest.raises(dnsapi.DnsError):
        digitalplat.set(DP, "vpn.example.dpdns.org")
    assert not [c for c in h.calls if c["method"] == "PATCH"]


@pytest.mark.parametrize("over, words", [
    ({("GET", "/domains"): (200, json.dumps({"success": True, "data": [{"domain": "other.dpdns.org"}]}))},
     "домен .* не найден"),
    ({("GET", "/domains/example.dpdns.org/dns/records"): (200, _dp_records())}, "A-записи .* нет"),
    ({("GET", "/domains/example.dpdns.org/dns/records"):
        (200, _dp_records(_DP_A, dict(_DP_A, id="R8", value="198.51.100.8")))}, "несколько A-записей .* оставь одну"),
    ({("PATCH", "/domains/example.dpdns.org/dns/records/R7"): (409, json.dumps({"success": False, "error": {
        "code": "etag_mismatch", "message": "Record was modified"}}))}, "Record was modified"),
    ({("GET", "/domains"): (401, json.dumps({"success": False, "error": {
        "code": "unauthorized", "message": "Invalid API key"}}))}, "Invalid API key"),
    ({("PATCH", "/domains/example.dpdns.org/dns/records/R7"): (200, json.dumps({"success": False}))}, "код 200"),
    ({("PATCH", "/domains/example.dpdns.org/dns/records/R7"): (502, "<html>Bad gateway</html>")}, "не JSON"),
])
def test_digitalplat_api_failures_are_dns_errors(monkeypatch, over, words):
    _Http(monkeypatch, digitalplat, _dp_routes(over))
    with pytest.raises(dnsapi.DnsError, match=words):
        digitalplat.set(DP, "203.0.113.9")


def test_digitalplat_record_with_a_cname_of_the_same_name_is_ignored(monkeypatch):
    """Среди записей имени — только A: CNAME/TXT с тем же именем не должны
    ни считаться «несколькими», ни подменять адрес."""
    txt = {"id": "T1", "type": "TXT", "name": "vpn", "value": "v=spf1 -all"}
    _Http(monkeypatch, digitalplat, _dp_routes({("GET", "/domains/example.dpdns.org/dns/records"):
                                                  (200, _dp_records(txt, _DP_A))}))
    assert digitalplat.read(DP) == "198.51.100.7"
