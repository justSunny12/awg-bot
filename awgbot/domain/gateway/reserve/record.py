"""
record.py — часть пакета reserve агента (см. __init__): факт записи DNS у
авторитетного сервера зоны (util/dnsapi/common) — опора, не зависящая от
слова резерва: сменить запись резерв может только конвертом DNS под главным
ключом, то есть после выдачи доли или кода. Кэш на минуту: пересылку
решает каждый такт, а запрос к авторитетным серверам — сеть.
"""
from __future__ import annotations

import time

_CACHE: dict[str, tuple[float, bool, float]] = {}
TTL = 60.0


def points_to(name: str, ip: str, now: float | None = None) -> bool | None:
    """Запись `name` у авторитетного сервера содержит `ip`; не прочиталась —
    прежний ответ, а без него — None («не знаю», пересылка не меняется).
    Срок кэша — минута ± 40 %: запрос к авторитетным серверам — сеть, ровный
    маячок с шлюза ни к чему."""
    import random
    from awgbot.util import dnsapi
    now = time.monotonic() if now is None else now
    key = f"{name}|{ip}"
    hit = _CACHE.get(key)
    if hit and now - hit[0] < hit[2]:
        return hit[1]
    try:
        ok = ip in dnsapi.read_authoritative(name)
    except dnsapi.DnsError:
        return hit[1] if hit else None
    _CACHE[key] = (now, ok, TTL * random.uniform(0.6, 1.4))
    return ok


def forget() -> None:
    _CACHE.clear()
