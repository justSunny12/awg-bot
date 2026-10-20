"""
record.py — часть пакета reserve агента (см. __init__): факт записи DNS у
авторитетного сервера зоны (util/dnsapi/common) — опора, не зависящая от
слова резерва: сменить запись резерв может только конвертом DNS под главным
ключом, то есть после выдачи доли или кода. Кэш на минуту: пересылку
решает каждый такт, а запрос к авторитетным серверам — сеть.
"""
from __future__ import annotations

import time

_CACHE: dict[str, tuple[float, bool]] = {}
TTL = 60.0


def points_to(name: str, ip: str, now: float | None = None) -> bool:
    """Запись `name` у авторитетного сервера содержит `ip`; не прочиталась —
    прежний ответ, а без него — False."""
    from awgbot.util import dnsapi
    now = time.monotonic() if now is None else now
    key = f"{name}|{ip}"
    hit = _CACHE.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    try:
        ok = ip in dnsapi.read_authoritative(name)
    except dnsapi.DnsError:
        return hit[1] if hit else False
    _CACHE[key] = (now, ok)
    return ok


def forget() -> None:
    _CACHE.clear()
