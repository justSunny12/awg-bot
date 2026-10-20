"""
pairproto.py — сообщения канала пары и гостевого канала: виды, поля, потолки; обе роли.

Оба канала живут внутри туннелей AWG и берут конверт канала шлюзов
(`util/gwlink.pack/unpack`: подпись HMAC, нонсы сессии, добивка) со своими
доменами ключа: канал пары — из закрытого ключа линка пары, гостевой канал —
из PSK постоянного линка шлюза к резерву. Здесь — только словарь: какие виды
бывают, кто их шлёт, какие поля обязательны и каких размеров, и потолки
частей: пакет и реплика крупнее фидов, поэтому частей больше, чем у канала
шлюзов (`MAX_CHUNKS`), а часть — та же (`gwlink.CHUNK`).

Разбор тела — `check(kind, body, side)`: неизвестный вид, вид не с той
стороны, нет обязательного поля, поле не того типа или длиннее потолка —
`ProtocolError`. Сессия рвётся, как у канала шлюзов: единственный источник на
том конце — наш же код, и мусор означает поломку или чужого.
"""
from __future__ import annotations

import hashlib

from awgbot.util import bundlecrypt
from awgbot.util.gwlink import CHUNK, ProtocolError

PAIR_PROTO = "pair1"
GUEST_PROTO = "rsv1"
MAX_CHUNKS = 256                   # 256 × 128 КБ = 32 МБ: потолок пакета 16 МБ сжатого с запасом
MAX_LINE = 256 * 1024              # как у канала шлюзов
PAD = 512
MAX_NAME = 64                      # имя сервера, имя линка, имя записи DNS
MAX_HOST = 253
MAX_REASON = 200
MAX_LIST = 64                      # подсети, адреса, слоты в одном сообщении
MAX_BLOB = CHUNK * 2               # одна часть шифротекста в hex

_PAIR_TAG = b"awgbot-pair-v1"
_GUEST_TAG = b"awgbot-rsvlink-v1"


def pair_key(pair_privkey_b64: str) -> bytes:
    """Ключ подписи канала пары — из закрытого ключа линка пары (у обеих сторон свой
    ключ, домен общий: подпись сверяется ключом ОТПРАВИТЕЛЯ, его открытый известен)."""
    return hashlib.sha256(_PAIR_TAG + bundlecrypt.derive_key(pair_privkey_b64)).digest()


def guest_key(link_psk_b64: str) -> bytes:
    """Ключ подписи гостевого канала — из PSK постоянного линка шлюза к резерву."""
    return hashlib.sha256(_GUEST_TAG + bundlecrypt.derive_key(link_psk_b64)).digest()


# ── словарь видов ─────────────────────────────────────────────────────────────
# (кто шлёт, обязательные поля: имя → (тип, потолок длины/значения или None))

INVITER, ACCEPTOR, BOTH = "inviter", "acceptor", "both"   # канал пары
RESERVE, GATEWAY = "reserve", "gateway"                   # гостевой канал

_S = str
_I = int
_L = list
_D = dict
_B = bool

PAIR_KINDS: dict[str, tuple[str, dict]] = {
    # сопряжение — до кода
    "hello":        (ACCEPTOR, {"pair": (_S, 16), "eph": (_S, 64), "name": (_S, MAX_NAME), "version": (_S, 24),
                                "caps": (_L, 16), "gen": (_I, None), "ports": (_L, 4), "seen": (_S, MAX_HOST),
                                "bot": (_S, MAX_NAME), "gw": (_B, None), "nonce": (_S, 32)}),
    "pair_eph":     (INVITER, {"eph": (_S, 64), "name": (_S, MAX_NAME), "version": (_S, 24), "caps": (_L, 16),
                               "gen": (_I, None), "ports": (_L, 4), "bot": (_S, MAX_NAME), "gw": (_B, None)}),
    "pair_wrong":   (INVITER, {"left": (_I, None)}),
    "pair_refused": (INVITER, {"reason": (_S, MAX_REASON)}),
    "pair_ok":      (INVITER, {"sealed": (_S, 4096)}),
    "pair_admin":   (ACCEPTOR, {"sealed": (_S, 4096)}),
    "link_next":    (BOTH, {"pub": (_S, 64), "port": (_I, None)}),
    "link_ack":     (BOTH, {}),
    "link_go":      (BOTH, {}),
    # обычная сессия
    "st":           (BOTH, {"ready": (_B, None), "name": (_S, MAX_NAME), "version": (_S, 24), "caps": (_L, 16),
                            "gen": (_I, None), "ports": (_L, 4), "self": (_S, MAX_HOST), "out": (_S, 16),
                            "in": (_S, 16), "pkg": (_D, None), "voters": (_L, MAX_LIST), "admin_id": (_I, None),
                            "admin_opened": (_B, None), "gw": (_B, None)}),
    "ping":         (BOTH, {"n": (_S, 32)}),
    "pong":         (BOTH, {"n": (_S, 32)}),
    "slots":        (BOTH, {"slots": (_L, MAX_LIST)}),
    "slots_ok":     (BOTH, {"slots": (_L, MAX_LIST)}),
    "pkg":          (BOTH, {"gen": (_I, None), "fp": (_S, 64), "i": (_I, None), "n": (_I, None),
                            "blob": (_S, MAX_BLOB), "share": (_S, 64), "m_gen": (_I, None)}),
    "pkg_ok":       (BOTH, {"gen": (_I, None), "fp": (_S, 64)}),
    "pkg_bad":      (BOTH, {"gen": (_I, None), "reason": (_S, MAX_REASON)}),
    "ask":          (BOTH, {"case": (_S, 8), "since": (_S, 32), "voters": (_L, MAX_LIST)}),
    "ask_no":       (BOTH, {"reason": (_S, 16)}),
    "go":           (BOTH, {"k": (_S, 64), "gen": (_I, None), "kind": (_S, 16)}),
    "up":           (BOTH, {}),
    "down":         (BOTH, {"reason": (_S, MAX_REASON)}),
    "released":     (BOTH, {"gen": (_I, None), "i": (_I, None), "n": (_I, None), "blob": (_S, MAX_BLOB)}),
    "taken":        (BOTH, {}),
    "blocks":       (BOTH, {"addrs": (_L, 4096)}),
    "back_req":     (BOTH, {}),
    "back":         (BOTH, {}),
    "tail":         (BOTH, {"i": (_I, None), "n": (_I, None), "blob": (_S, MAX_BLOB)}),
    "relay":        (BOTH, {"until": (_S, 32)}),
    "wiped":        (BOTH, {}),
    "evict":        (BOTH, {}),
    "view":         (BOTH, {"case": (_S, 8), "since": (_S, 32), "voters": (_L, MAX_LIST)}),
    "gone":         (BOTH, {}),
}

GUEST_KINDS: dict[str, tuple[str, dict]] = {
    "hello":  (GATEWAY, {"slot": (_I, None), "caps": (_L, 16), "m_gen": (_I, None), "seen": (_S, 32),
                         "nonce": (_S, 32)}),
    "seen":   (GATEWAY, {"seen": (_S, 32)}),
    "view":   (RESERVE, {"seen": (_S, 32), "voters": (_L, MAX_LIST), "in": (_S, 16), "pkg_gen": (_I, None)}),
    "need":   (RESERVE, {"case": (_S, 8), "since": (_S, 32)}),
    "share":  (GATEWAY, {"share": (_S, 64), "m_gen": (_I, None)}),
    "state":  (RESERVE, {"state": (_S, 16), "move": (_I, None)}),
    "ping":   (RESERVE, {"n": (_S, 32)}),
    "pong":   (GATEWAY, {"n": (_S, 32)}),
}

_SIDES = {
    PAIR_PROTO: {INVITER: (INVITER, BOTH), ACCEPTOR: (ACCEPTOR, BOTH)},
    GUEST_PROTO: {RESERVE: (RESERVE,), GATEWAY: (GATEWAY,)},
}


def kinds_for(proto: str) -> dict[str, tuple[str, dict]]:
    if proto == PAIR_PROTO:
        return PAIR_KINDS
    if proto == GUEST_PROTO:
        return GUEST_KINDS
    raise ValueError(f"неизвестный канал {proto!r}")


def check(proto: str, kind: str, body: dict, sender: str) -> dict:
    """Проверить тело сообщения вида `kind` от стороны `sender`; вернуть тело
    без служебных полей конверта. Неверно — ProtocolError."""
    spec = kinds_for(proto).get(kind)
    if spec is None:
        raise ProtocolError(f"неизвестный вид сообщения {kind!r}")
    who, fields = spec
    allowed = _SIDES[proto].get(sender)
    if allowed is None or who not in allowed:
        raise ProtocolError(f"сообщение {kind!r} не с той стороны")
    out = {}
    for name, (typ, cap) in fields.items():
        if name not in body:
            raise ProtocolError(f"в {kind!r} нет поля {name!r}")
        val = body[name]
        if typ is _I and isinstance(val, bool) or not isinstance(val, typ):
            raise ProtocolError(f"поле {name!r} в {kind!r} не того вида")
        if cap is not None and isinstance(val, (str, list)) and len(val) > cap:
            raise ProtocolError(f"поле {name!r} в {kind!r} длиннее предела")
        out[name] = val
    return out


def chunks(blob_hex: str) -> list[str]:
    """Шифротекст (hex) частями по CHUNK знаков; больше MAX_CHUNKS — ProtocolError."""
    parts = [blob_hex[i:i + MAX_BLOB] for i in range(0, len(blob_hex), MAX_BLOB)] or [""]
    if len(parts) > MAX_CHUNKS:
        raise ProtocolError("тело длиннее предела частей")
    return parts
