"""
gwsign.py — подписанные сообщения между основным ботом и агентом шлюза.

Токен пометки агент отправляет серверу каналом сам (вид `claim`). Токен
должен быть неподделываемым: случайный текст с похожей строкой не имеет
права пометить устройство шлюзом. Общий
секрет у сторон уже есть — приватный ключ линка: ВПС держит его в
gw-<линк>.conf, шлюз — в awglink.conf. Из него выводится ключ HMAC (поверх
bundlecrypt.derive_key, со своей строкой домена).

Формат одной строкой:
    GW1:<base64url(JSON)>.<base64url(HMAC-SHA256[:20])>
JSON: {"act": "claim", "pub": <ключ аплинка>, "ts": <unix>, "nonce": <hex>,
       "host": <имя машины>[, "caps": [<возможность>, …]]}
caps — что агент умеет по каналу (контракт канала).
Единственное действие — claim: агент просит пометить своё устройство шлюзом.
Лишние поля в JSON (агенты до 4.0.0 подписывали ещё и "addr") проверке не
мешают — подпись покрывает весь payload, а читаются только нужные ключи.
Токен приходит только полем `token` сообщения канала (ручной приём
пересланного сообщения снят вместе с шимами ниже минимума).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time

from awgbot.util import bundlecrypt

PREFIX = "GW1:"
# Окно свежести токена. Семь суток — наследие пересылки токена человеком; по каналу
# claim приходит сразу, и окно держит только расхождение часов сторон (шлюз без
# RTC до синхронизации) и повтор claim после долгого разрыва канала. Нонс
# повторный приём отсекает отдельно (MarkMixin._gw_nonce_seen).
MAX_AGE_SECONDS = 7 * 24 * 3600
_TOKEN_RE = re.compile(r"GW1:([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)")


def _key(link_privkey_b64: str) -> bytes:
    return hashlib.sha256(b"awgbot-gwsign-v1" + bundlecrypt.derive_key(link_privkey_b64)).digest()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(link_privkey_b64: str, act: str, pub: str, host: str = "", caps: tuple[str, ...] = ()) -> str:
    body = {"act": act, "pub": pub, "ts": int(time.time()), "nonce": os.urandom(8).hex(), "host": host[:64]}
    if caps:
        body["caps"] = list(caps)
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    mac = hmac.new(_key(link_privkey_b64), payload, hashlib.sha256).digest()[:20]
    return PREFIX + b64u(payload) + "." + b64u(mac)


def verify(link_privkey_b64: str, token: str, now: float | None = None) -> dict:
    """Разобрать и проверить токен. ValueError — не наш, битый, просрочен."""
    m = _TOKEN_RE.search(token or "")
    if not m:
        raise ValueError("в сообщении нет токена шлюза")
    try:
        payload = unb64u(m.group(1))
        mac = unb64u(m.group(2))
    except (ValueError, TypeError) as e:
        raise ValueError("токен повреждён") from e
    want = hmac.new(_key(link_privkey_b64), payload, hashlib.sha256).digest()[:20]
    if not hmac.compare_digest(mac, want):
        raise ValueError("подпись не сходится — сообщение не от этого шлюза или ключ линка другой")
    try:
        data = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError("токен повреждён") from e
    if data.get("act") != "claim" or not data.get("pub") or not data.get("nonce"):
        raise ValueError("токен неполный")
    caps = data.get("caps")
    data["caps"] = [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else []
    ts = int(data.get("ts") or 0)
    now = time.time() if now is None else now
    if not (now - MAX_AGE_SECONDS <= ts <= now + 3600):
        raise ValueError("токен просрочен — пусть шлюз выдаст новый")
    return data
