"""
nets.py — подсети: пересечение с учётом вложенности; адреса и имена.

Одна функция на сервис и тексты: «одинаковые» подсети — частный случай,
192.168.0.0/16 против 192.168.1.0/24 тоже пересечение. Здесь же — проверка
IP-литерала и разрешение имени в IPv4 с потолком ожидания (одно место вместо
копий по ролям).
"""
from __future__ import annotations

import ipaddress
import socket
from concurrent import futures

# Разрешение имён с потолком ожидания: у getaddrinfo своего таймаута нет, а
# socket.setdefaulttimeout его не ограничивает и меняет умолчание всему
# процессу. Просроченный вызов бросаем на произвол: поток отвиснет сам, а
# ждавший в очереди снимается — к резолверу уже не пойдёт и свежих за собой
# не задержит.
RESOLVE_TIMEOUT = 3.0
_resolve_pool = futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="awg-resolve")


def getaddrinfo_timed(host: str, *, timeout: float = RESOLVE_TIMEOUT, family: int = 0,
                      proto: int = socket.IPPROTO_TCP):
    """socket.getaddrinfo(host) не дольше timeout; futures.TimeoutError — не успел."""
    fut = _resolve_pool.submit(socket.getaddrinfo, host, None, family=family, proto=proto)
    try:
        return fut.result(timeout=timeout)
    except futures.TimeoutError:
        fut.cancel()                                  # ещё в очереди — снят; уже идёт — отвиснет сам
        raise


def resolve4(host: str, timeout: float = RESOLVE_TIMEOUT) -> str:
    """Первый IPv4 имени в порядке ответа резолвера; пусто — не разрешилось
    или не уложилось в потолок. Умолчание таймаута сокетов процесса не трогает."""
    try:
        infos = getaddrinfo_timed(host, timeout=timeout, family=socket.AF_INET)
    except (futures.TimeoutError, OSError, UnicodeError):
        return ""
    for info in infos or ():
        addr = str(info[4][0])
        if is_ipv4(addr):
            return addr
    return ""


def is_ip(host: str) -> bool:
    """IP-литерал (IPv4 или IPv6), а не имя."""
    try:
        ipaddress.ip_address(str(host))
        return True
    except ValueError:
        return False


def is_ipv4(tok: str) -> bool:
    """IPv4-литерал."""
    try:
        return ipaddress.ip_address(str(tok)).version == 4
    except ValueError:
        return False


def normalize(nets) -> list[str]:
    """Подсети строками в каноне («10.9.1.0/24»), без повторов; мусор — мимо."""
    out: list[str] = []
    for n in parse(nets):
        s = str(n)
        if s not in out:
            out.append(s)
    return out


def parse(nets) -> list[ipaddress.IPv4Network]:
    """Подсети объектами; нечитаемые — мимо (strict=False: адрес хоста → сеть)."""
    out = []
    for n in nets or []:
        try:
            out.append(ipaddress.IPv4Network(str(n).strip(), strict=False))
        except ValueError:
            continue
    return out


def overlap(a: list[str], b: list[str]) -> list[str]:
    """Подсети из a, пересекающиеся с какой-либо из b; мусор пропускается."""
    out = []
    for x in a:
        try:
            nx = ipaddress.ip_network(x, strict=False)
        except ValueError:
            continue
        for y in b:
            try:
                if nx.overlaps(ipaddress.ip_network(y, strict=False)):
                    out.append(x)
                    break
            except ValueError:
                continue
    return out


__all__ = ["overlap"]
