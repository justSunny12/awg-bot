"""Сеть в тестах закрыта (tests/netguard.py, автоматические фикстуры conftest
`_no_network` и `_names_resolve_at_once`).

Стережём: попытка разрешить имя или соединиться не с петлёй — немедленная
ошибка (без ожидания таймаута) и запись попытки, по которой фикстура роняет
тест, даже если код ошибку поймал; петля и unix-сокеты — как обычно; чтение
записей DNS и разрешение с потолком отвечают сразу, даже когда общий пул
резолвера процесса занят.

Цена ошибки: тест, тихо ушедший в сеть, на машине без сети ждёт секунды
таймаута (точечные прогоны резерва шли минутами), а с сетью зависит от чужого
резолвера; подставной резолвер, ответ которого идёт через общий пул из двух
потоков с потолком 3 с, под нагрузкой «не успевает» — плавающий провал,
которого в коде нет.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
import threading
import time

import pytest

from tests import netguard

pytestmark = pytest.mark.unit


def _denied(fn) -> tuple[BaseException, list[str], float]:
    """Вызов, который сторож обязан отклонить: исключение, попытки, время."""
    t0 = time.monotonic()
    with pytest.raises(OSError) as e:
        fn()
    took = time.monotonic() - t0
    return e.value, netguard.take(), took        # take — попытка проверена, фикстура теста её не видит


def test_an_outbound_tcp_connect_fails_at_once_and_is_recorded():
    err, tried, took = _denied(lambda: socket.create_connection(("203.0.113.1", 443), timeout=5))
    assert isinstance(err, netguard.NetworkClosed), err
    assert tried and "203.0.113.1" in tried[0], tried
    assert took < 1, f"отказ через {took:.1f} с — сторож ждал сети"


def test_resolving_a_name_fails_at_once_while_literals_and_localhost_resolve():
    err, tried, took = _denied(lambda: socket.getaddrinfo("vpn.example.org", 443))
    assert isinstance(err, socket.gaierror) and tried == ["getaddrinfo → 'vpn.example.org'"], tried
    assert took < 1
    _denied(lambda: socket.gethostbyname("vpn.example.org"))
    assert socket.getaddrinfo("203.0.113.9", 443)[0][4][0] == "203.0.113.9"
    assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
    socket.getaddrinfo("localhost", 80)
    assert netguard.take() == [], "литерал или localhost сочтены выходом в сеть"


def test_a_udp_datagram_out_is_refused_and_one_to_loopback_goes():
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        err, tried, _ = _denied(lambda: out.sendto(b"q", ("198.51.100.53", 53)))
        assert tried == ["sendto → ('198.51.100.53', 53)"], tried
        _denied(lambda: out.connect(("198.51.100.53", 53)))
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        out.sendto(b"ok", rx.getsockname())
        assert rx.recvfrom(16)[0] == b"ok"
    finally:
        out.close()
        rx.close()
    assert netguard.take() == []


def test_loopback_tcp_and_unix_sockets_work():
    srv = socket.create_server(("127.0.0.1", 0))
    try:
        with socket.create_connection(srv.getsockname(), timeout=2):
            conn, _ = srv.accept()
            conn.close()
    finally:
        srv.close()
    d = tempfile.mkdtemp(prefix="ng")                  # короткий путь: у unix-сокета предел ~104 байт
    try:
        path = os.path.join(d, "s")
        u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        u.bind(path)
        u.listen(1)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(path)
        c.close()
        u.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert netguard.take() == [], "петля или unix-сокет сочтены выходом в сеть"


def test_an_asyncio_connection_out_is_refused_at_once():
    async def go():
        await asyncio.wait_for(asyncio.open_connection("203.0.113.1", 443), 5)
    err, tried, took = _denied(lambda: asyncio.run(go()))
    assert tried and took < 1, (tried, took)


def test_a_swallowed_attempt_is_still_recorded():
    """Код ловит OSError и идёт своим путём отказа — попытка всё равно на
    учёте: по ней фикстура роняет тест."""
    try:
        socket.create_connection(("203.0.113.1", 80), timeout=5)
    except OSError:
        pass
    assert netguard.take() == ["connect → ('203.0.113.1', 80)"]


def test_dns_records_read_at_once_as_without_network():
    """Чтение записей без подстановки теста: авторитетный «не ответил»
    (DnsError), системный резолвер имени не знает — сразу, без попытки."""
    from awgbot.util import dnsapi
    t0 = time.monotonic()
    with pytest.raises(dnsapi.DnsError):
        dnsapi.read_authoritative("vpn-a.example.org")
    with pytest.raises(dnsapi.DnsError):
        dnsapi.read_authoritative_ttl("vpn-a.example.org")
    assert dnsapi.read_system("vpn-a.example.org") == []
    assert time.monotonic() - t0 < 1
    assert netguard.take() == []


def test_a_substituted_resolver_answers_even_when_the_shared_pool_is_busy(monkeypatch):
    """Оба потока общего пула заняты (повисшие вызовы соседей) — подставной
    резолвер теста всё равно отвечает сразу: вызов идёт не через пул."""
    from awgbot.util import nets
    release = threading.Event()
    busy = [nets._resolve_pool.submit(release.wait, 10) for _ in range(4)]
    try:
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("203.0.113.9", 0))])
        t0 = time.monotonic()
        assert nets.getaddrinfo_timed("dyn.example.org", timeout=0.5)[0][4][0] == "203.0.113.9"
        assert time.monotonic() - t0 < 0.5, "ответ ждал общего пула"
    finally:
        release.set()
        for f in busy:
            f.result(5)


@pytest.mark.resolve_pool
def test_the_real_cap_runs_on_its_own_pool_per_test(monkeypatch):
    """Тест настоящего потолка получает свой пул: общий пул процесса занят
    повисшими вызовами — его срок от этого не зависит."""
    from awgbot.util import nets
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("203.0.113.9", 0))])
    assert nets.getaddrinfo_timed("dyn.example.org", timeout=2)[0][4][0] == "203.0.113.9"
    assert nets._resolve_pool._thread_name_prefix == "awg-resolve-test", "тест потолка — на общем пуле процесса"
