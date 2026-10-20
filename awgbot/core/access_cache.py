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


# «👤 Меню клиента» у админа второго сервера: на время сессии он — клиент этого
# сервера (шаг роли в middleware пропускается), потом снова роль
PEER_CLIENT_TTL = 30 * 60.0
_peer_client: dict[int, float] = {}


def peer_client_on(uid: int) -> None:
    _peer_client[int(uid)] = time.monotonic() + PEER_CLIENT_TTL


def peer_client_off(uid: int) -> None:
    _peer_client.pop(int(uid), None)


def peer_client_active(uid: int) -> bool:
    until = _peer_client.get(int(uid))
    if until is None:
        return False
    if until < time.monotonic():
        _peer_client.pop(int(uid), None)
        return False
    return True
