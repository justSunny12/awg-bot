"""Unit: `awgbot.util.net4` — исходящие HTTP(S)-запросы только по IPv4.

Правило проекта: наружу только IPv4. Резолвер отдаёт многим хостам и AAAA,
и штатный urllib пробует v6 первым — на шлюзе соединение уходит мимо
туннеля, на ВПС в никуда, и каждый запрос к GitHub, спискам или Bot API
стоит секунды перебора. `connect4` берёт у резолвера только A-записи и
перебирает их; `net4.urlopen` — тот же urllib, но с таким соединением.

Сеть — подмена `socket.getaddrinfo` и `socket.create_connection` через
`_Host`; в `urlopen`-тестах подменён сам `connect4`, чтобы видеть, что путь
идёт через него, а не через штатный connect http.client.
"""
from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest

from awgbot.util import net4

pytestmark = pytest.mark.unit

_V4A = ("203.0.113.10", 443)
_V4B = ("203.0.113.11", 443)
_V6 = ("2001:db8::10", 443, 0, 0)


class _Host:
    """Резолвер и TCP хоста под тест.

    `records` — что «знает DNS» о хосте: список (семейство, адрес); фейковый
    getaddrinfo честно фильтрует по запрошенному семейству, как настоящий.
    `refuse` — адреса, у которых connect падает; `lookups` и `connects` —
    что у хоста спросили и куда пошли."""

    def __init__(self, monkeypatch, records, refuse=()):
        self.records = list(records)
        self.refuse = set(refuse)
        self.lookups: list[tuple] = []
        self.connects: list[tuple] = []
        monkeypatch.setattr(socket, "getaddrinfo", self._getaddrinfo)
        monkeypatch.setattr(socket, "create_connection", self._create_connection)

    def _getaddrinfo(self, host, port, family=0, type=0, proto=0, flags=0):
        self.lookups.append((host, port, family, type))
        return [(fam, socket.SOCK_STREAM, 6, "", sa)
                for fam, sa in self.records if family in (0, fam)]

    def _create_connection(self, address, timeout=None, source_address=None):
        self.connects.append((address, timeout, source_address))
        if address[0] in self.refuse:
            raise ConnectionRefusedError(111, f"refused {address[0]}")
        return ("sock", address)


# ── connect4 ─────────────────────────────────────────────────────────────────

def test_connect4_asks_the_resolver_only_for_ipv4_and_takes_the_first_address(monkeypatch):
    """У резолвера запрашиваются только A-записи (AF_INET, SOCK_STREAM): хост с
    AAAA впереди не даёт v6-адреса, соединение идёт по первому v4 с тем же
    таймаутом и исходным адресом, что передал вызывающий."""
    host = _Host(monkeypatch, [(socket.AF_INET6, _V6), (socket.AF_INET, _V4A), (socket.AF_INET, _V4B)])
    sock = net4.connect4("api.example.net", 443, 7.5, ("10.0.0.2", 0))
    assert host.lookups == [("api.example.net", 443, socket.AF_INET, socket.SOCK_STREAM)], \
        "резолвер спрошен не только про IPv4 — v6-адрес попал бы в перебор"
    assert host.connects == [(_V4A, 7.5, ("10.0.0.2", 0))], \
        "соединение не по первому IPv4-адресу или потерян таймаут/исходный адрес"
    assert sock == ("sock", _V4A)


def test_connect4_falls_through_to_the_next_address_when_the_first_refuses(monkeypatch):
    """Первый адрес отказал — пробуется второй, и вызывающий получает сокет, а
    не ошибку: у GitHub и Telegram по несколько A-записей, одна мёртвая не
    должна ронять запрос."""
    host = _Host(monkeypatch, [(socket.AF_INET, _V4A), (socket.AF_INET, _V4B)], refuse={_V4A[0]})
    assert net4.connect4("h", 443, 5) == ("sock", _V4B)
    assert [a for a, *_ in host.connects] == [_V4A, _V4B], "после отказа первого адреса второй не пробовался"


def test_connect4_raises_the_last_error_when_every_address_refuses(monkeypatch):
    """Все адреса отказали — наружу уходит последняя ошибка соединения (её
    текст попадёт в журнал и в «не ответил» у вызывающего), лишних попыток нет."""
    host = _Host(monkeypatch, [(socket.AF_INET, _V4A), (socket.AF_INET, _V4B)], refuse={_V4A[0], _V4B[0]})
    with pytest.raises(OSError, match=f"refused {_V4B[0]}"):
        net4.connect4("h", 443, 5)
    assert len(host.connects) == 2, "адреса перебирались не по одному разу"


def test_connect4_without_an_ipv4_address_is_a_clear_error_and_no_connect(monkeypatch):
    """У хоста только AAAA (или ничего) — понятная ошибка с именем хоста, и
    ни одной попытки соединения: по v6 ходить нельзя, а молчаливый None
    развалился бы дальше в http.client непонятным сообщением."""
    host = _Host(monkeypatch, [(socket.AF_INET6, _V6)])
    with pytest.raises(OSError, match="у v6only.example.net нет IPv4-адреса"):
        net4.connect4("v6only.example.net", 443, 5)
    assert host.connects == [], "без IPv4-адреса соединение всё же пробовалось"


# ── urlopen ──────────────────────────────────────────────────────────────────

def _trace_connect4(monkeypatch):
    """connect4, который записывает (host, port, timeout) и обрывает соединение
    понятной ошибкой — по ней видно, что urlopen пришёл именно сюда."""
    seen: list[tuple] = []

    def fake(host, port, timeout, source_address=None):
        seen.append((host, port, timeout))
        raise OSError("connect4-stub")
    monkeypatch.setattr(net4, "connect4", fake)
    return seen


def test_urlopen_over_https_connects_through_connect4_with_host_port_and_timeout(monkeypatch):
    """`https://` идёт через наше соединение: хост и порт 443 из адреса,
    таймаут вызова доходит до connect, а ошибка соединения приходит
    вызывающему как URLError с тем же текстом — тем, что ловят github/feeds/
    dnsapi."""
    seen = _trace_connect4(monkeypatch)
    with pytest.raises(urllib.error.URLError, match="connect4-stub"):
        net4.urlopen("https://api.example.net/releases", timeout=7.5)
    assert seen == [("api.example.net", 443, 7.5)], \
        "https-запрос ушёл не через connect4 либо потерял хост/порт/таймаут"


def test_urlopen_over_http_connects_through_connect4_too(monkeypatch):
    """Обычный `http://` — тоже через connect4, порт 80 и явный порт из адреса."""
    seen = _trace_connect4(monkeypatch)
    with pytest.raises(urllib.error.URLError, match="connect4-stub"):
        net4.urlopen("http://lists.example.net/ip.txt", timeout=3)
    with pytest.raises(urllib.error.URLError, match="connect4-stub"):
        net4.urlopen("http://lists.example.net:8080/ip.txt", timeout=3)
    assert seen == [("lists.example.net", 80, 3), ("lists.example.net", 8080, 3)]


def test_urlopen_accepts_a_prepared_request_object(monkeypatch):
    """Вызывающие передают `urllib.request.Request` с заголовками и методом
    (feeds, dnsapi, github) — хост и порт берутся из него, путь тот же."""
    seen = _trace_connect4(monkeypatch)
    req = urllib.request.Request("https://dns.example.net/v1/zones", headers={"User-Agent": "awg-bot"}, method="PATCH")
    with pytest.raises(urllib.error.URLError, match="connect4-stub"):
        net4.urlopen(req, timeout=3.0)
    assert seen == [("dns.example.net", 443, 3.0)]


def test_urlopen_passes_the_caller_timeout_to_the_connection_unchanged(monkeypatch):
    """Каждый вызывающий ставит свой предел (GETME_TIMEOUT, FETCH_TIMEOUT,
    dnsapi.TIMEOUT) — до соединения доходит ровно он, а не умолчание urllib:
    иначе правило «наружу не дольше 10 с» выполняется только на бумаге."""
    seen = _trace_connect4(monkeypatch)
    for tmo in (1.0, 4, 10.0):
        with pytest.raises(urllib.error.URLError):
            net4.urlopen("https://api.example.net/", timeout=tmo)
    assert [t for _, _, t in seen] == [1.0, 4, 10.0], "таймаут вызова не дошёл до соединения"


def test_urlopen_end_to_end_resolves_only_ipv4_even_when_the_resolver_knows_aaaa(monkeypatch):
    """Без подмены connect4: от urlopen до резолвера. Хост с AAAA впереди —
    у резолвера спросили только A, соединяться пошли по v4; обрыв на
    соединении после этого — ожидаемая URLError, в сеть не ходим."""
    host = _Host(monkeypatch, [(socket.AF_INET6, _V6), (socket.AF_INET, _V4A)], refuse={_V4A[0]})
    with pytest.raises(urllib.error.URLError, match="refused"):
        net4.urlopen("https://api.example.net/", timeout=2)
    assert host.lookups == [("api.example.net", 443, socket.AF_INET, socket.SOCK_STREAM)]
    assert [a for a, *_ in host.connects] == [_V4A], "urlopen пошёл не по IPv4-адресу"


# ── почта: IMAP4_SSL4 и SMTP4 ────────────────────────────────────────────────

class _Ctx:
    """Подмена ssl.SSLContext: запоминает, что и с каким SNI обернули."""

    def __init__(self):
        self.wrapped: list[tuple] = []

    def wrap_socket(self, sock, server_hostname=None):
        self.wrapped.append((sock, server_hostname))
        return ("tls", sock)


def _trace_connect4_ok(monkeypatch):
    """connect4, который записывает аргументы и отдаёт «сокет»."""
    seen: list[tuple] = []

    def fake(host, port, timeout, source_address=None):
        seen.append((host, port, timeout, source_address))
        return ("sock", host, port)
    monkeypatch.setattr(net4, "connect4", fake)
    return seen


def test_imap_over_tls_connects_through_connect4_and_keeps_sni_by_host_name(monkeypatch):
    """Ящик кодов и проверка почты: сокет IMAP — от connect4 с хостом, портом и
    таймаутом вызова, TLS поверх него с SNI и проверкой имени по хосту —
    иначе почтовый сервер отдаёт не тот сертификат, и вход падает."""
    seen = _trace_connect4_ok(monkeypatch)
    ctx = _Ctx()
    conn = net4.IMAP4_SSL4.__new__(net4.IMAP4_SSL4)
    conn.host, conn.port, conn.ssl_context = "imap.example.org", 993, ctx
    sock = net4.IMAP4_SSL4._create_socket(conn, 10)
    assert seen == [("imap.example.org", 993, 10, None)], "IMAP-сокет не от connect4 или потерял таймаут"
    assert ctx.wrapped == [(("sock", "imap.example.org", 993), "imap.example.org")], \
        "TLS не поверх сокета connect4 либо SNI не по имени хоста"
    assert sock == ("tls", ("sock", "imap.example.org", 993))


def test_smtp_connects_through_connect4_with_the_source_address(monkeypatch):
    """Отправка и проверка SMTP: сокет — от connect4 с хостом, портом, таймаутом
    и исходным адресом сессии; таймаут 0 (неблокирующий сокет) отвергается,
    как у smtplib."""
    seen = _trace_connect4_ok(monkeypatch)
    s = net4.SMTP4()                              # без хоста — соединения в конструкторе нет
    s.source_address = ("10.0.0.2", 0)
    assert s._get_socket("smtp.example.org", 587, 10.0) == ("sock", "smtp.example.org", 587)
    assert seen == [("smtp.example.org", 587, 10.0, ("10.0.0.2", 0))], "SMTP-сокет не от connect4"
    with pytest.raises(ValueError):
        s._get_socket("smtp.example.org", 587, 0)
    assert len(seen) == 1, "при timeout=0 соединение всё же пробовалось"


def test_urlopen_without_an_explicit_timeout_uses_the_project_limit(monkeypatch):
    """Вызов без timeout — не «ждать вечно», а предел правила проекта: 10 с
    доходят до соединения. Иначе один забытый аргумент — и такт висит на
    молчащем сервере, пока его не убьют."""
    assert net4.TIMEOUT_S == 10.0, "умолчание net4 разошлось с правилом «не дольше 10 с»"
    seen = _trace_connect4(monkeypatch)
    with pytest.raises(urllib.error.URLError, match="connect4-stub"):
        net4.urlopen("https://api.example.net/")
    assert seen == [("api.example.net", 443, 10.0)], "без явного timeout до соединения дошло не 10 с"
