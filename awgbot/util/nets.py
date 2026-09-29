"""
nets.py — подсети: пересечение с учётом вложенности.

Одна функция на сервис и тексты: «одинаковые» подсети — частный случай,
192.168.0.0/16 против 192.168.1.0/24 тоже пересечение.
"""
from __future__ import annotations

import ipaddress
import socket
from concurrent import futures

# Разрешение имён с потолком ожидания: у getaddrinfo своего таймаута нет, а
# socket.setdefaulttimeout его не ограничивает и меняет умолчание всему
# процессу. Просроченный вызов бросаем на произвол: поток отвиснет сам.
RESOLVE_TIMEOUT = 3.0
_resolve_pool = futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="awg-resolve")


def getaddrinfo_timed(host: str, *, timeout: float = RESOLVE_TIMEOUT, family: int = 0,
                      proto: int = socket.IPPROTO_TCP):
    """socket.getaddrinfo(host) не дольше timeout; futures.TimeoutError — не успел."""
    fut = _resolve_pool.submit(socket.getaddrinfo, host, None, family=family, proto=proto)
    return fut.result(timeout=timeout)


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
