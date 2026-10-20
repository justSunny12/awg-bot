"""
joinsas.py — код подтверждения сопряжения (SAS) и ключ сопряжения, обе роли.

Обе стороны держат по эфемерной паре X25519 (только в памяти процесса) и
обмениваются открытыми ключами в канале линка на ключах токена (hello{eph} и
join_eph{eph}). Общий секрет K = X25519(свой закрытый, чужой открытый):

    SAS = BLAKE2b("awgbot-join-sas-v1" ‖ id ‖ slot ‖ eph шлюза ‖ eph сервера ‖ K)[:30 бит]
    Kp  = BLAKE2b("awgbot-join-key-v1" ‖ id ‖ K)        — ключ SecretBox сопряжения

Код — 6 знаков Crockford base32 (без I, L, O, U), показ `K7F-2QX`. Ввод
прощает руки: регистр, пробелы, дефисы, точки и подчёркивания не важны,
`O` → `0`, `I`/`L` → `1`; не 6 знаков алфавита — None (это не попытка).
Сравнение — constant-time.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Optional

from nacl import bindings
from nacl.public import PrivateKey
from nacl.secret import SecretBox

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LENGTH = 6
_SAS_TAG = b"awgbot-join-sas-v1"
_KEY_TAG = b"awgbot-join-key-v1"


def new_pair() -> tuple[bytes, bytes]:
    """(закрытый, открытый) эфемерной пары X25519."""
    k = PrivateKey.generate()
    return bytes(k), bytes(k.public_key)


def shared(priv: bytes, peer_pub: bytes) -> bytes:
    if len(priv) != 32 or len(peer_pub) != 32:
        raise ValueError("эфемерный ключ не 32 байта")
    return bindings.crypto_scalarmult(priv, peer_pub)


PAIR_SAS_TAG = b"awgbot-pair-sas-v1"    # сопряжение серверов (пара): свой домен — тот же вход даёт другой код
PAIR_KEY_TAG = b"awgbot-pair-key-v1"


def code(join_id: str, slot: int | None, gw_eph: bytes, srv_eph: bytes, k: bytes, tag: bytes = _SAS_TAG) -> str:
    """Код подтверждения: 30 бит → 6 знаков. `slot` None — без байта слота
    (пара серверов); `tag` — домен: шлюз или пара."""
    slot_b = b"" if slot is None else bytes([int(slot) & 0xFF])
    d = hashlib.blake2b(tag + bytes.fromhex(join_id) + slot_b + gw_eph + srv_eph + k,
                        digest_size=16).digest()
    n = int.from_bytes(d[:4], "big") >> 2           # 30 бит
    out = []
    for _ in range(LENGTH):
        out.append(ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def pair_key(join_id: str, k: bytes, tag: bytes = _KEY_TAG) -> bytes:
    return hashlib.blake2b(tag + bytes.fromhex(join_id) + k, digest_size=32).digest()


def show(c: str) -> str:
    """«K7F2QX» → «K7F-2QX»."""
    return f"{c[:3]}-{c[3:]}" if len(c) == LENGTH else c


_DROP = str.maketrans("", "", " \t-._")
_FIX = str.maketrans({"O": "0", "I": "1", "L": "1"})


def normalize(text: str) -> Optional[str]:
    """Ввод человека → код из 6 знаков алфавита или None (не тот формат)."""
    s = str(text or "").upper().translate(_DROP).translate(_FIX)
    if len(s) != LENGTH or any(ch not in ALPHABET for ch in s):
        return None
    return s


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def seal(key: bytes, data: dict) -> str:
    """Секреты после кода — под SecretBox(Kp): hex-строкой в теле join_ok."""
    return SecretBox(key).encrypt(json.dumps(data, separators=(",", ":")).encode()).hex()


def unseal(key: bytes, blob: str) -> dict:
    """Расшифровать; чужой ключ или порча — ValueError."""
    from nacl.exceptions import CryptoError
    try:
        raw = SecretBox(key).decrypt(bytes.fromhex(str(blob or "")))
        data = json.loads(raw.decode())
    except (CryptoError, ValueError, UnicodeDecodeError) as e:
        raise ValueError("секреты сопряжения не расшифровались") from e
    if not isinstance(data, dict):
        raise ValueError("секреты сопряжения не того вида")
    return data
