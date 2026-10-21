"""
wgkeys.py — ключи и параметры обфускации AmneziaWG без `awg genkey`: пара
Curve25519 (та же математика, что у WireGuard; открытый ключ из закрытого —
scalarmult базовой точки с тем же прижатием), PSK — 32 случайных байта,
обфускация `Jc…H4` — как у скриптов поставки (`gen_obf` в
reserve-pair-setup.sh: S1 + 56 ≠ S2, диапазоны H без пересечений).
"""
from __future__ import annotations

import base64
import secrets

from nacl.public import PrivateKey


def keypair() -> tuple[str, str]:
    """(закрытый, открытый) — base64, как в конфигах."""
    k = PrivateKey.generate()
    return base64.b64encode(bytes(k)).decode(), base64.b64encode(bytes(k.public_key)).decode()


def pubkey_of(priv_b64: str) -> str:
    """Публичный ключ из закрытого; не ключ — ValueError (wgconf.pubkey_of отдаёт пусто)."""
    from awgbot.util import wgconf
    pub = wgconf.pubkey_of(priv_b64)
    if not pub:
        raise ValueError("закрытый ключ — 32 байта base64")
    return pub


def psk() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _rnd(lo: int, hi: int) -> int:
    return lo + secrets.randbelow(hi - lo + 1)


def _h_range(lo: int, hi: int) -> str:
    start = _rnd(lo, hi - 200000)
    return f"{start}-{start + _rnd(50000, 150000)}"


def obfuscation() -> dict[str, str]:
    """Jc, Jmin, Jmax, S1…S4, H1…H4 — строками, как в конфиге."""
    s1, s2 = _rnd(15, 150), _rnd(15, 150)
    while s1 + 56 == s2:
        s2 = _rnd(15, 150)
    return {"Jc": str(_rnd(3, 10)), "Jmin": str(_rnd(8, 15)), "Jmax": str(_rnd(40, 70)),
            "S1": str(s1), "S2": str(s2), "S3": str(_rnd(15, 150)), "S4": str(_rnd(15, 150)),
            "H1": _h_range(5, 500000000), "H2": _h_range(500000001, 1000000000),
            "H3": _h_range(1000000001, 1500000000), "H4": _h_range(1500000001, 2000000000)}


OBF_KEYS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4")
