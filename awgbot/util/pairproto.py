"""
pairproto.py — сообщения канала пары и гостевого канала: виды, поля, потолки; обе роли.

Оба канала живут внутри туннелей AWG и берут конверт канала шлюзов
(`util/gwlink.pack/unpack`: подпись HMAC, нонсы сессии, добивка) со своими
доменами ключа: канал пары — из закрытого ключа линка пары, гостевой канал —
из PSK постоянного линка шлюза к резерву. Здесь — только словарь: какие виды
бывают, кто их шлёт, какие поля обязательны и каких размеров, и потолки
частей: пакет и реплика крупнее фидов, поэтому частей больше, чем у канала
шлюзов (`MAX_CHUNKS`), а часть — 64 КБ шифротекста (в hex и base64 конверта
она укладывается в предел строки `MAX_LINE`).

Разбор тела — `check(kind, body, side)`: неизвестный вид, вид не с той
стороны, нет обязательного поля, поле не того типа или длиннее потолка —
`ProtocolError`. Сессия рвётся, как у канала шлюзов: единственный источник на
том конце — наш же код, и мусор означает поломку или чужого.
"""
from __future__ import annotations

import hashlib

from awgbot.util import bundlecrypt
from awgbot.util.gwlink import ProtocolError

PAIR_PROTO = "pair1"
GUEST_PROTO = "rsv1"
MAX_CHUNKS = 256                   # 256 × 64 КБ = 16 МБ — потолок пакета
MAX_LINE = 256 * 1024              # как у канала шлюзов
MAX_NAME = 64                      # имя сервера, имя линка, имя записи DNS
MAX_HOST = 253
MAX_REASON = 200
DEFAULT_PAIR_PORT = 8788                 # порт канала пары в /30 линка (app.reserve.pair_channel_port)


def pair_hosts(cidr: str) -> tuple[str, str]:
    """(адрес принявшего, адрес пригласившего) в /30 линка пары: первый и
    второй хост — так их раздаёт скрипт линка пары."""
    import ipaddress
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return "", ""
    if net.version != 4 or net.prefixlen != 30:
        return "", ""
    hosts = [str(h) for h in net.hosts()]
    return hosts[0], hosts[1]
MAX_LIST = 64                      # подсети, адреса, слоты в одном сообщении
CHUNK_BYTES = 64 * 1024            # часть шифротекста: в hex 128 К знаков, в конверте (base64) — в предел строки
MAX_BLOB = CHUNK_BYTES * 2         # одна часть шифротекста в hex

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
    # обычная сессия
    "st":           (BOTH, {"ready": (_B, None), "name": (_S, MAX_NAME), "version": (_S, 24), "caps": (_L, 16),
                            "gen": (_I, None), "ports": (_L, 4), "self": (_S, MAX_HOST), "out": (_S, 16),
                            "in": (_S, 16), "pkg": (_D, None), "voters": (_L, MAX_LIST), "admin_id": (_I, None),
                            "admin_opened": (_B, None), "gw": (_B, None),
                            "healthy": (_B, None), "can_return": (_B, None), "in_kind": (_S, 16), "mem_free": (_I, None), "online": (_I, None), "in_by": (_S, 8),
                            "zone": (_S, 120), "jrn": (_D, None), "quo": (_D, None)}),
    "ping":         (BOTH, {"n": (_S, 32)}),
    "pong":         (BOTH, {"n": (_S, 32)}),
    "slots":        (BOTH, {"slots": (_L, MAX_LIST)}),
    "slots_ok":     (BOTH, {"slots": (_L, MAX_LIST)}),
    "pkg":          (BOTH, {"gen": (_I, None), "fp": (_S, 64), "i": (_I, None), "n": (_I, None),
                            "blob": (_S, MAX_BLOB), "share": (_S, 64), "m_gen": (_I, None), "jrn": (_D, None)}),
    "pkg_ok":       (BOTH, {"gen": (_I, None), "fp": (_S, 64)}),
    "pkg_bad":      (BOTH, {"gen": (_I, None), "reason": (_S, MAX_REASON)}),
    "ask":          (BOTH, {"case": (_S, 8), "since": (_S, 32), "voters": (_L, MAX_LIST)}),
    "ask_no":       (BOTH, {"reason": (_S, 16)}),
    # переезд кнопкой: `go_req` — запусти процесс открытия и дай разовый ключ; `open_eph` —
    # его публичная часть и идентификатор открытия; `go` — ключ пакета конвертом к нему
    # (`sealed` + `oid`); `k` открытым — только от основы прежнего выпуска
    "go_req":       (BOTH, {"gen": (_I, None), "kind": (_S, 16), "ttl": (_I, None)}),
    "open_eph":     (BOTH, {"gen": (_I, None), "eph": (_S, 64), "oid": (_S, 16)}),
    "go":           (BOTH, {"k": (_S, 64), "gen": (_I, None), "kind": (_S, 16), "ttl": (_I, None),
                            "sealed": (_S, 384), "oid": (_S, 16)}),   # конверт: 48 + JSON (K, gen до 10 цифр, oid) ≤ 162 байт
    "up":           (BOTH, {}),
    "down":         (BOTH, {"reason": (_S, MAX_REASON)}),
    "released":     (BOTH, {"gen": (_I, None), "i": (_I, None), "n": (_I, None), "blob": (_S, MAX_BLOB),
                            "epoch": (_I, None), "last": (_I, None)}),
    "taken":        (BOTH, {}),
    "blocks":       (BOTH, {"addrs": (_L, 4096)}),
    "back_req":     (BOTH, {}),
    "back":         (BOTH, {}),
    "relay":        (BOTH, {"until": (_S, 32)}),
    "wiped":        (BOTH, {}),
    "evict":        (BOTH, {}),
    "view":         (BOTH, {"case": (_S, 8), "since": (_S, 32), "voters": (_L, MAX_LIST)}),
    "gone":         (BOTH, {}),
    # кворум резерва (quo1): состав линии и подтверждение, конфиг зонда второму серверу, точки и
    # ящики ключа, взгляд голосующего, эпоха и роль держателя, плановая пауза лидера
    "qmem":         (BOTH, {"l": (_S, 1), "g": (_I, None), "members": (_L, MAX_LIST)}),
    "qmem_ok":      (BOTH, {"l": (_S, 1), "g": (_I, None)}),
    "qprobe":       (BOTH, {"l": (_S, 1), "g": (_I, None), "conf": (_S, 4096)}),
    "qkeys":        (BOTH, {"l": (_S, 1), "m_gen": (_I, None), "q_gen": (_I, None), "t": (_I, None), "x": (_I, None),
                            "y": (_S, 64), "qbox": (_S, 256), "c_gen": (_I, None), "cy": (_S, 64), "cbox": (_S, 256)}),
    "qkeys_ok":     (BOTH, {"l": (_S, 1), "q_gen": (_I, None), "c_gen": (_I, None)}),
    "qview":        (BOTH, {"l": (_S, 1), "g": (_I, None), "w": (_S, 8), "since": (_S, 32), "e": (_I, None), "i": (_D, None)}),
    "qstate":       (BOTH, {"l": (_S, 1), "e": (_I, None), "holder": (_S, 8), "r": (_S, 8)}),
    "qpause":       (BOTH, {"l": (_S, 1), "until": (_S, 32)}),
    # журнал данных (jrn1, util/rsvjrn): линия — db_line базы, чьи это данные; записи — под K
    # с открытым заголовком (rsvjrn.event). jev/jack/jneed/jstale — события держателя линии,
    # jcnt/jcack — пачки счётчиков стороны a (основа) или b (клон на резерве)
    "jev":          (BOTH, {"line": (_S, 16), "e": (_I, None), "from": (_I, None), "n": (_I, None),
                            "blob": (_S, MAX_BLOB)}),
    "jack":         (BOTH, {"line": (_S, 16), "i": (_I, None)}),
    "jneed":        (BOTH, {"line": (_S, 16), "from": (_I, None)}),
    "jstale":       (BOTH, {"line": (_S, 16), "e": (_I, None), "F": (_I, None)}),
    "jcnt":         (BOTH, {"line": (_S, 16), "side": (_S, 1), "s": (_I, None), "blob": (_S, MAX_BLOB)}),
    "jcack":        (BOTH, {"line": (_S, 16), "side": (_S, 1), "s": (_I, None)}),
    # хвост старой эпохи: бывший держатель → держатель (события под K частями); ответ — числа и,
    # если перебазировал бот основы на резерве, полный список спорного под K (частями)
    "jtail":        (BOTH, {"line": (_S, 16), "e": (_I, None), "F": (_I, None), "part": (_I, None), "parts": (_I, None),
                            "blob": (_S, MAX_BLOB)}),
    "jtail_ok":     (BOTH, {"line": (_S, 16), "e": (_I, None), "F": (_I, None), "moved": (_I, None),
                            "disputed_n": (_I, None), "more": (_I, None), "part": (_I, None), "parts": (_I, None),
                            "blob": (_S, MAX_BLOB)}),
}

# виды журнала: по каналу пары разбирает journal/route (`reserve_jrn_in`), по локальному каналу
# гостя — те же имена (guestctl)
JRN_KINDS: tuple[str, ...] = ("jev", "jcnt", "jack", "jneed", "jstale", "jcack", "jtail", "jtail_ok")

GUEST_KINDS: dict[str, tuple[str, dict]] = {
    "hello":  (GATEWAY, {"slot": (_I, None), "caps": (_L, 16), "m_gen": (_I, None), "seen": (_S, 32),
                         "nonce": (_S, 32)}),
    "seen":   (GATEWAY, {"seen": (_S, 32)}),
    "view":   (RESERVE, {"seen": (_S, 32), "voters": (_L, MAX_LIST), "in": (_S, 16), "pkg_gen": (_I, None)}),
    "need":   (RESERVE, {"case": (_S, 8), "since": (_S, 32), "m_gen": (_I, None)}),
    "share":  (GATEWAY, {"share": (_S, 64), "m_gen": (_I, None)}),
    "state":  (RESERVE, {"state": (_S, 16), "move": (_I, None), "reason": (_S, MAX_REASON), "e": (_I, None)}),
    "ping":   (BOTH, {"n": (_S, 32)}),                    # проба по подозрению — с любой стороны
    "pong":   (BOTH, {"n": (_S, 32)}),
    # кворум (quo1): взгляд шлюза, опрос и слова кандидата, точка ключа кворума, пауза, эпоха
    "qview":  (GATEWAY, {"l": (_S, 1), "g": (_I, None), "w": (_S, 8), "since": (_S, 32), "e": (_I, None), "i": (_D, None)}),
    "qask":   (RESERVE, {"l": (_S, 1), "g": (_I, None)}),
    "qtake":  (RESERVE, {"l": (_S, 1), "e": (_I, None)}),
    "qrel":   (RESERVE, {"l": (_S, 1), "e": (_I, None)}),
    "qneed":  (RESERVE, {"l": (_S, 1), "q_gen": (_I, None), "since": (_S, 32)}),
    "qshare": (GATEWAY, {"l": (_S, 1), "q_gen": (_I, None), "x": (_I, None), "y": (_S, 64)}),
    "qpause": (RESERVE, {"l": (_S, 1), "until": (_S, 32)}),
    "qstate": (RESERVE, {"l": (_S, 1), "e": (_I, None), "holder": (_S, 8), "r": (_S, 8)}),
}

_SIDES = {
    PAIR_PROTO: {INVITER: (INVITER, BOTH), ACCEPTOR: (ACCEPTOR, BOTH)},
    GUEST_PROTO: {RESERVE: (RESERVE, BOTH), GATEWAY: (GATEWAY, BOTH)},
}


def kinds_for(proto: str) -> dict[str, tuple[str, dict]]:
    if proto == PAIR_PROTO:
        return PAIR_KINDS
    if proto == GUEST_PROTO:
        return GUEST_KINDS
    raise ValueError(f"неизвестный канал {proto!r}")


# поля, которых может не быть (добавлены внутри линии; отсутствие — ложь/пусто)
OPTIONAL: dict[str, frozenset] = {"st": frozenset({"healthy", "can_return", "in_kind", "mem_free", "online", "in_by", "zone",
                                                     "jrn", "quo"}),
                                  "need": frozenset({"m_gen"}), "state": frozenset({"reason", "e"}), "qview": frozenset({"i"}),
                                  "go": frozenset({"ttl", "k", "sealed", "oid"}), "go_req": frozenset({"ttl"}),
                                  "pkg": frozenset({"jrn"}), "released": frozenset({"epoch", "last"}),
                                  "jstale": frozenset({"F"}),
                                  # hello обычной сессии несёт только nonce (и seen); поля сопряжения — у hello с pair
                                  "hello": frozenset({"pair", "eph", "name", "version", "caps", "gen", "ports", "seen", "bot", "gw"})}


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
            if name in OPTIONAL.get(kind, ()):
                continue                                   # поле появилось позже первого выпуска линии
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
