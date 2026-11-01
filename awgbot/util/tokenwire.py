"""
tokenwire.py — общий кадр строковых токенов: подключение шлюза (`AWGJ1.…`,
util/jointoken.py) и приглашение в пару серверов (`AWGP1.…`, util/pairtoken.py).

Кадр одинаков у обоих видов: `<ПРЕФИКС><base64url без =>` от тела и
контрольной суммы BLAKE2b (4 байта) тела; первый байт тела — версия формата.
Внутри тела общие куски — выпуск X.Y.Z.P (4 байта, P = 0 — без хотфикса), хост
длиной и ≤ 63 байт [A-Za-z0-9.-], базовая обфускация линка Jc, Jmin, Jmax,
S1–S4 (по байту) и H1–H4 (нижняя и верхняя граница, uint32 big-endian). Поля
и их порядок — в модулях видов; здесь только кадр и общие куски.

Двоичная запись не меняется: установщик на bash (install/awg-bot-install.sh,
join_parse) читает токен подключения по тем же смещениям.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct

FORMAT = 1
HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,63}\Z")
SUM = 4
OBF_LEN = 3 + 4 + 32          # Jc, Jmin, Jmax; S1–S4; H1–H4
_B64 = r"[A-Za-z0-9_-]{40,400}"


class TokenError(ValueError):
    """Токен не разобрался: не наш, повреждён, другой версии формата."""


# ── куски тела ───────────────────────────────────────────────────────────────

def version_bytes(version: str, where: str) -> bytes:
    """Выпуск «X.Y.Z» или «X.Y.Z.P» — 4 байта; не укладывается — TokenError."""
    try:
        parts = [int(x) for x in str(version).lstrip("v").split(".")]
    except ValueError:
        parts = []
    if not 3 <= len(parts) <= 4 or any(not 0 <= p <= 255 for p in parts):
        raise TokenError(f"выпуск {version!r} не укладывается в {where}")
    parts += [0] * (4 - len(parts))
    return bytes(parts)


def version_str(raw: bytes) -> str:
    x, y, z, p = raw
    return f"{x}.{y}.{z}" + (f".{p}" if p else "")


def host_ok(host) -> bool:
    return isinstance(host, str) and bool(HOST_RE.match(host))


def pack_host(host: str) -> bytes:
    """Хост длиной и байтами; правило хоста проверяет вызывающий (host_ok)."""
    raw = host.encode("ascii")
    return struct.pack(">B", len(raw)) + raw


def unpack_host(body: bytes, p: int) -> tuple[str, int]:
    """Хост с позиции p → (хост, позиция после него); IndexError/UnicodeDecodeError — порча."""
    hlen = body[p]
    return body[p + 1:p + 1 + hlen].decode("ascii"), p + 1 + hlen


def pack_obf(jc: int, jmin: int, jmax: int, s: tuple, h: tuple) -> bytes:
    return (struct.pack(">BBB", jc, jmin, jmax) + struct.pack(">BBBB", *s)
            + b"".join(struct.pack(">II", lo, hi) for lo, hi in h))


def unpack_obf(body: bytes, p: int) -> tuple[int, int, int, tuple, tuple, int]:
    """Jc…H4 с позиции p → (jc, jmin, jmax, s, h, позиция после)."""
    jc, jmin, jmax = body[p:p + 3]
    p += 3
    s4 = tuple(body[p:p + 4])
    p += 4
    hs = tuple(struct.unpack(">II", body[p + 8 * i:p + 8 * i + 8]) for i in range(4))
    return jc, jmin, jmax, s4, hs, p + 32


def obfuscation(tok) -> dict:
    """Jc…H4 токена строками — как их пишет скрипт линка."""
    out = {"Jc": str(tok.jc), "Jmin": str(tok.jmin), "Jmax": str(tok.jmax)}
    for i, v in enumerate(tok.s, 1):
        out[f"S{i}"] = str(v)
    for i, (lo, hi) in enumerate(tok.h, 1):
        out[f"H{i}"] = f"{lo}-{hi}"
    return out


# ── кадр ─────────────────────────────────────────────────────────────────────

def _sum(body: bytes) -> bytes:
    return hashlib.blake2b(body, digest_size=SUM).digest()


def wrap(prefix: str, body: bytes) -> str:
    """Тело → `<prefix><base64url без =>` тела и его суммы."""
    return prefix + base64.urlsafe_b64encode(body + _sum(body)).decode().rstrip("=")


def unwrap(prefix: str, text: str, *, min_body: int, not_ours: str, damaged: str, newer: str) -> bytes:
    """Строка → тело с проверенной суммой и версией формата. Тексты отказов —
    свои у каждого вида: не тот префикс, порча, другая версия формата."""
    s = str(text or "").strip()
    if not s.startswith(prefix):
        raise TokenError(not_ours)
    b64 = s[len(prefix):]
    if not re.fullmatch(_B64, b64):
        raise TokenError(damaged)
    try:
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except (ValueError, TypeError) as e:
        raise TokenError(damaged) from e
    if len(raw) < min_body + SUM:
        raise TokenError(damaged)
    body, got = raw[:-SUM], raw[-SUM:]
    if not hmac.compare_digest(_sum(body), got):
        raise TokenError(damaged)
    if body[0] != FORMAT:
        raise TokenError(newer)
    return body


def find(prefix: str, text: str) -> str:
    """Первая строка вида `<prefix>…` в тексте или пусто."""
    m = re.search(re.escape(prefix) + _B64, str(text or ""))
    return m.group(0) if m else ""
