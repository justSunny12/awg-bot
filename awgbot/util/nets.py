"""
nets.py — подсети: пересечение с учётом вложенности.

Одна функция на сервис и тексты: «одинаковые» подсети — частный случай,
192.168.0.0/16 против 192.168.1.0/24 тоже пересечение (docs/gateway-lan.md §4.2).
"""
from __future__ import annotations

import ipaddress


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
