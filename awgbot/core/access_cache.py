"""
access_cache.py — кэш «кто это» для middleware: tg_id → (роль, клиент, устройство).

Middleware ходил в БД синхронно в event loop на КАЖДЫЙ апдейт (у друга — три
запроса, у постороннего — два). Кэш с коротким TTL закрывает это; любая запись
в БД (коммит транзакции) сбрасывает его целиком — пользователей единицы, и
«сбросить всё» дешевле, чем помнить, кто от чего зависит.
"""
from __future__ import annotations

import time

TTL_SECONDS = 15.0
_cache: dict[int, tuple[float, str, object, object]] = {}


def get(uid: int):
    hit = _cache.get(uid)
    if hit is None or hit[0] < time.monotonic():
        return None
    return hit[1], hit[2], hit[3]


def put(uid: int, role: str, client, device=None) -> None:
    _cache[uid] = (time.monotonic() + TTL_SECONDS, role, client, device)


def invalidate_all() -> None:
    _cache.clear()
