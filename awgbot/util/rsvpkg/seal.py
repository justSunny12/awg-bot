"""
seal.py — упаковка пакета переезда: заголовок, `K` под `M`, части под `K`,
конверт DNS под `M`, отпечаток, добивка до ступени; обе стороны.

Двоичная запись:

    4  метка «AWGK»          1  версия формата (1)
    4  поколение пакета      4  поколение M
    2+ SecretBox_M(K): длина и шифротекст (16 + 24 + 32 байта)
    4+ конверт DNS: длина и SecretBox_M(zlib(JSON)); 0 — конверта нет
    4+ части под K: число, затем на каждую — 1+ имя, 4+ длина, SecretBox_K(zlib(JSON))
    …  добивка нулями до ступени STEP
   32  отпечаток BLAKE2b всего выше (открыт; по нему сверяют поколение и целостность)

Ключ SecretBox из `M` — `rsvkeys.box_key`; `K` — 32 случайных байта. Открыть
заголовок (поколения, отпечаток, размер) может любой; `K` — тот, у кого `M`
(процесс открытия на резерве после сборки долей) или кому `K` отдали
(переезд кнопкой); конверт DNS — только с `M`.

Переезд кнопкой: `K` идёт основой не открытым, а конвертом к разовому ключу
процесса открытия (`seal_k_to` / `open_sealed_k`): SealedBox к публичной
части X25519, которую процесс выпускает на одно открытие; внутри — `K`,
поколение пакета и идентификатор открытия, чтобы конверт не подошёл ни к
другому пакету, ни к другому запуску процесса. Бот резерва видит только
шифротекст.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import struct
import zlib
from dataclasses import dataclass

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.secret import SecretBox

from awgbot.util import rsvkeys

MAGIC = b"AWGK"
FORMAT = 1
STEP = 64 * 1024
MAX_SIZE = 16 * 1024 * 1024
K_LEN = 32
_DIGEST = 32
_PERSON = b"awgbot-rsvpkg-v1"


class PackageError(ValueError):
    """Пакет не разобрался: чужой ключ, порча, не тот формат, слишком большой."""


@dataclass(frozen=True)
class Header:
    pkg_gen: int
    m_gen: int
    size: int            # полный размер пакета (со ступенью)
    digest: str          # hex
    has_dns: bool


def new_k() -> bytes:
    return secrets.token_bytes(K_LEN)


EPH_LEN = 32                     # публичная часть разового ключа процесса открытия
OID_LEN = 16                     # идентификатор открытия — hex


def new_eph() -> tuple[bytes, bytes]:
    """(закрытая, открытая) — разовая пара X25519 процесса открытия; закрытая
    живёт только в памяти процесса."""
    k = PrivateKey.generate()
    return bytes(k), bytes(k.public_key)


def seal_k_to(eph_pub: bytes, k: bytes, pkg_gen: int, oid: str) -> bytes:
    """Конверт `K` к публичной части разового ключа процесса открытия (переезд
    кнопкой): внутри — `K`, поколение пакета и идентификатор открытия."""
    if len(eph_pub) != EPH_LEN:
        raise PackageError("разовый ключ не 32 байта")
    if len(k) != K_LEN:
        raise PackageError("ключ пакета не той длины")
    body = json.dumps({"k": k.hex(), "gen": int(pkg_gen), "oid": str(oid)}, separators=(",", ":")).encode()
    return bytes(SealedBox(PublicKey(eph_pub)).encrypt(body))


def open_sealed_k(eph_priv: bytes, sealed: bytes, pkg_gen: int, oid: str) -> bytes:
    """`K` из конверта к своей разовой паре; не тот ключ, порча, чужое
    поколение пакета или чужой идентификатор открытия — PackageError."""
    try:
        raw = bytes(SealedBox(PrivateKey(eph_priv)).decrypt(sealed))
        obj = json.loads(raw.decode())
        k = bytes.fromhex(str(obj.get("k") or ""))
    except (CryptoError, ValueError, TypeError) as e:
        raise PackageError("ключ пакета не подходит — не тот ключ или порча") from e
    if int(obj.get("gen") or 0) != int(pkg_gen):
        newer = int(obj.get("gen") or 0) > int(pkg_gen)
        raise PackageError("ключ пакета — ключ от " + ("более нового" if newer else "прошлого") + " поколения")
    if str(obj.get("oid") or "") != str(oid):
        raise PackageError("ключ пакета: не к этому процессу открытия")
    if len(k) != K_LEN:
        raise PackageError("ключ пакета не той длины")
    return k


def _box(key: bytes, data: bytes) -> bytes:
    return bytes(SecretBox(key).encrypt(data))


def _unbox(key: bytes, data: bytes, what: str) -> bytes:
    try:
        return bytes(SecretBox(key).decrypt(data))
    except (CryptoError, ValueError) as e:
        raise PackageError(f"{what}: не тот ключ или порча") from e


def _pack_json(obj) -> bytes:
    return zlib.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode(), 6)


def _unpack_json(raw: bytes, what: str):
    try:
        return json.loads(zlib.decompress(raw).decode())
    except (zlib.error, ValueError, UnicodeDecodeError) as e:
        raise PackageError(f"{what}: не разобралось") from e


def seal(parts: dict[str, object], *, k: bytes, m: bytes, pkg_gen: int, m_gen: int,
         dns: dict | None = None) -> bytes:
    """Собрать пакет. `parts` — имя → объект JSON (часть VPN, часть бота…)."""
    if len(k) != K_LEN:
        raise PackageError(f"ключ пакета — {K_LEN} байт")
    mkey = rsvkeys.box_key(m)
    body = MAGIC + struct.pack(">BII", FORMAT, int(pkg_gen), int(m_gen))
    ek = _box(mkey, k)
    body += struct.pack(">H", len(ek)) + ek
    env = _box(mkey, _pack_json(dns)) if dns else b""
    body += struct.pack(">I", len(env)) + env
    body += struct.pack(">I", len(parts))
    for name, obj in parts.items():
        nm = str(name).encode("ascii")
        if not 1 <= len(nm) <= 255:
            raise PackageError("имя части — 1…255 знаков ASCII")
        ct = _box(k, _pack_json(obj))
        body += struct.pack(">B", len(nm)) + nm + struct.pack(">I", len(ct)) + ct
    total = len(body) + _DIGEST
    pad = (-total) % STEP
    body += b"\x00" * pad
    if len(body) + _DIGEST > MAX_SIZE:
        raise PackageError("пакет больше предела 16 МБ")
    return body + hashlib.blake2b(body, digest_size=_DIGEST, person=_PERSON).digest()


def _check(blob: bytes) -> bytes:
    if len(blob) < 4 + 9 + 2 + 4 + 4 + _DIGEST or len(blob) > MAX_SIZE:
        raise PackageError("пакет не того размера")
    body, got = blob[:-_DIGEST], blob[-_DIGEST:]
    if hashlib.blake2b(body, digest_size=_DIGEST, person=_PERSON).digest() != got:
        raise PackageError("отпечаток пакета не сошёлся")
    if body[:4] != MAGIC or body[4] != FORMAT:
        raise PackageError("это не пакет переезда или другой формат")
    return body


def _walk(body: bytes):
    """(pkg_gen, m_gen, ek, env, [(имя, шифротекст)…])."""
    pkg_gen, m_gen = struct.unpack(">II", body[5:13])
    p = 13
    (ln,) = struct.unpack(">H", body[p:p + 2]); p += 2
    ek = body[p:p + ln]; p += ln
    (ln,) = struct.unpack(">I", body[p:p + 4]); p += 4
    env = body[p:p + ln]; p += ln
    (n,) = struct.unpack(">I", body[p:p + 4]); p += 4
    parts = []
    for _ in range(n):
        nl = body[p]; p += 1
        name = body[p:p + nl].decode("ascii", errors="replace"); p += nl
        (ln,) = struct.unpack(">I", body[p:p + 4]); p += 4
        parts.append((name, body[p:p + ln])); p += ln
    if p > len(body):
        raise PackageError("пакет оборван")
    return pkg_gen, m_gen, ek, env, parts


def header(blob: bytes) -> Header:
    """Открытая часть: поколения, размер, отпечаток, есть ли конверт DNS."""
    body = _check(blob)
    try:
        pkg_gen, m_gen, _ek, env, _parts = _walk(body)
    except (struct.error, IndexError) as e:
        raise PackageError("пакет оборван") from e
    return Header(pkg_gen=pkg_gen, m_gen=m_gen, size=len(blob), digest=blob[-_DIGEST:].hex(), has_dns=bool(env))


def open_k(blob: bytes, m: bytes) -> bytes:
    """`K` из заголовка — ключом `M`."""
    _pkg, _mg, ek, _env, _parts = _walk(_check(blob))
    k = _unbox(rsvkeys.box_key(m), ek, "ключ пакета")
    if len(k) != K_LEN:
        raise PackageError("ключ пакета не той длины")
    return k


def open_dns(blob: bytes, m: bytes) -> dict | None:
    """Конверт DNS — ключом `M`; None — конверта нет."""
    _pkg, _mg, _ek, env, _parts = _walk(_check(blob))
    if not env:
        return None
    data = _unpack_json(_unbox(rsvkeys.box_key(m), env, "конверт DNS"), "конверт DNS")
    if not isinstance(data, dict):
        raise PackageError("конверт DNS не того вида")
    return data


def open_parts(blob: bytes, k: bytes) -> dict[str, object]:
    """Части пакета — ключом `K`."""
    _pkg, _mg, _ek, _env, parts = _walk(_check(blob))
    out = {}
    for name, ct in parts:
        out[name] = _unpack_json(_unbox(k, ct, f"часть {name}"), f"часть {name}")
    return out


def digest(blob: bytes) -> str:
    return blob[-_DIGEST:].hex() if len(blob) >= _DIGEST else ""


def snapshot_seal(k: bytes, obj: object) -> bytes:
    """Шифрованный снимок бота основы на резерве (`rep.bin`) — под тем же `K`."""
    return _box(k, _pack_json(obj))


def snapshot_open(k: bytes, blob: bytes):
    return _unpack_json(_unbox(k, blob, "снимок"), "снимок")
