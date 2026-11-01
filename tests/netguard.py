"""Сеть в тестах закрыта: сторож исходящих соединений и разрешения имён.

Что стережём. Тест, дошедший до настоящего DNS-запроса или соединения
наружу, на машине без сети ждёт таймаута (сопряжение резерва фиксирует
собственный адрес у авторитетного сервера записи — каждый тест пары стоял
секунды), а с сетью — зависит от чужого резолвера и отдаёт имена тестовых
зон наружу. Цена ошибки — точечные прогоны минутами и плавающие провалы
под нагрузкой; поэтому выход в сеть — провал теста, даже если код поймал
ошибку и пошёл дальше.

Как устроено. Ставится один раз на импорте conftest (до awgbot): подменены
`socket.getaddrinfo`, `gethostbyname(_ex)`, `gethostbyaddr`, `getnameinfo` и
у `socket.socket` — `connect`, `connect_ex`, `sendto`, `sendmsg`. Разрешено:
имена `localhost`, IP-литералы (разрешаются без сети), петля и «любой адрес»
(127.0.0.0/8, ::1, 0.0.0.0, ::), unix-сокеты и прочие семейства, кроме
AF_INET/AF_INET6. Остальное — немедленная ошибка (`NetworkClosed`, у
разрешения имён — `ResolveClosed`, оба — OSError: код идёт своим путём
отказа без ожидания) и запись попытки; автоматическая фикстура conftest
роняет тест, у которого попытки были. Подмены тестов поверх (свой
`socket.getaddrinfo`, свой `socket.socket`) работают как прежде: откат
monkeypatch возвращает сюда, а не к настоящим функциям.
"""
from __future__ import annotations

import ipaddress
import socket
import threading

_lock = threading.Lock()
attempts: list[str] = []          # попытки текущего теста: «что → куда»
_installed = False


class NetworkClosed(OSError):
    """Соединение наружу в тестах запрещено."""


class ResolveClosed(socket.gaierror):
    """Разрешение имени через сеть в тестах запрещено."""


def _host_str(host) -> str:
    if isinstance(host, (bytes, bytearray)):
        return bytes(host).decode("ascii", "replace")
    return "" if host is None else str(host)


def _ip(host: str):
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def local_host(host) -> bool:
    """Адрес не выводит наружу: петля, «любой адрес», имя localhost."""
    h = _host_str(host).strip().strip("[]")
    if h in ("", "localhost", "localhost."):
        return True
    ip = _ip(h)
    return ip is not None and (ip.is_loopback or ip.is_unspecified)


def _resolvable_offline(host) -> bool:
    """Разрешение имени не пойдёт в сеть: пусто, localhost или IP-литерал."""
    h = _host_str(host).strip().strip("[]")
    return local_host(h) or _ip(h) is not None


def _deny(kind: str, target, exc=NetworkClosed):
    what = f"{kind} → {target!r}"
    with _lock:
        attempts.append(what)
    raise exc(f"сеть в тестах закрыта: {what}")


def _addr_ok(sock, address) -> bool:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return True
    if not isinstance(address, tuple) or not address:
        return True
    return local_host(address[0])


def take() -> list[str]:
    """Попытки с последнего вызова (и сброс)."""
    with _lock:
        out = list(attempts)
        attempts.clear()
    return out


def install() -> None:
    """Поставить сторож (повторный вызов — без действия)."""
    global _installed
    if _installed:
        return
    _installed = True
    real_gai = socket.getaddrinfo
    real_gbn = socket.gethostbyname
    real_gbn_ex = socket.gethostbyname_ex
    real_gba = socket.gethostbyaddr
    real_gni = socket.getnameinfo
    cls = socket.socket
    real_connect, real_connect_ex = cls.connect, cls.connect_ex
    real_sendto, real_sendmsg = cls.sendto, cls.sendmsg

    def getaddrinfo(host, port, *a, **kw):
        if not _resolvable_offline(host):
            _deny("getaddrinfo", host, ResolveClosed)
        return real_gai(host, port, *a, **kw)

    def gethostbyname(host):
        if not _resolvable_offline(host):
            _deny("gethostbyname", host, ResolveClosed)
        return real_gbn(host)

    def gethostbyname_ex(host):
        if not _resolvable_offline(host):
            _deny("gethostbyname_ex", host, ResolveClosed)
        return real_gbn_ex(host)

    def gethostbyaddr(host):
        if not local_host(host):
            _deny("gethostbyaddr", host, ResolveClosed)
        return real_gba(host)

    def getnameinfo(sockaddr, flags):
        numeric = (flags & socket.NI_NUMERICHOST) and (flags & socket.NI_NUMERICSERV)
        if not numeric and not local_host(sockaddr[0] if sockaddr else ""):
            _deny("getnameinfo", sockaddr, ResolveClosed)
        return real_gni(sockaddr, flags)

    def connect(self, address):
        if not _addr_ok(self, address):
            _deny("connect", address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if not _addr_ok(self, address):
            _deny("connect_ex", address)
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        address = args[-1] if args else None
        if address is not None and not _addr_ok(self, address):
            _deny("sendto", address)
        return real_sendto(self, data, *args)

    def sendmsg(self, buffers, *args):
        address = args[2] if len(args) > 2 else None
        if address is not None and not _addr_ok(self, address):
            _deny("sendmsg", address)
        return real_sendmsg(self, buffers, *args)

    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.gethostbyaddr = gethostbyaddr
    socket.getnameinfo = getnameinfo
    cls.connect, cls.connect_ex = connect, connect_ex
    cls.sendto, cls.sendmsg = sendto, sendmsg


# ── подставной мир имён тестов ───────────────────────────────────────────────

def quiet_dns(monkeypatch) -> None:
    """Чтение записей (util/dnsapi) — сразу и без сети, как на машине без
    сети, только без ожидания таймаутов: авторитетный сервер «не отвечает»
    (DnsError), системный резолвер имени не знает (пусто). Сопряжение резерва
    фиксирует собственный адрес именно так (reserve_pin_self_host) — каждый
    тест пары иначе шёл бы к настоящему резолверу. Тест со своим ответом
    подменяет эти имена поверх; сам DNS-клиент проверяют тесты с меткой
    `real_dns` (подмена сокета и резолвера в tests/unit/test_dnsapi.py)."""
    from awgbot.util import dnsapi

    def authoritative_ttl(name, timeout=0.0):
        raise dnsapi.DnsError(f"сеть в тестах закрыта: авторитетные серверы {name} не ответили")

    monkeypatch.setattr(dnsapi, "read_authoritative_ttl", authoritative_ttl)
    monkeypatch.setattr(dnsapi, "read_authoritative", lambda name, timeout=0.0: authoritative_ttl(name)[0])
    monkeypatch.setattr(dnsapi, "read_system", lambda name, timeout=0.0: [])


def resolve_at_once(monkeypatch) -> None:
    """Разрешение имён с потолком (util/nets.getaddrinfo_timed) — прямым
    вызовом `socket.getaddrinfo` в потоке теста, без общего пула процесса (два
    потока, потолок 3 с): подставной резолвер теста отвечает сразу, а под
    нагрузкой поток пула не успевал к сроку — пустой ответ и провал, которого
    в коде нет. Без подстановки вызов упирается в сторож сети. Сам потолок
    проверяют тесты с меткой `resolve_pool` (там — `real_resolve_pool`)."""
    from awgbot.util import nets

    def at_once(host, *, timeout=0.0, family=0, proto=socket.IPPROTO_TCP):
        return socket.getaddrinfo(host, None, family=family, proto=proto)

    monkeypatch.setattr(nets, "getaddrinfo_timed", at_once)


def real_resolve_pool(monkeypatch):
    """Настоящий getaddrinfo_timed — на своём пуле теста: повисший вызов
    соседа (тест потолка отпускает его не сразу) не занимает его потоки."""
    from concurrent import futures
    from awgbot.util import nets
    pool = futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="awg-resolve-test")
    monkeypatch.setattr(nets, "_resolve_pool", pool)
    return pool
