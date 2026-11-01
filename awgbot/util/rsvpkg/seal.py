"""
seal.py — упаковка пакета переезда: заголовок, `K` под `M`, части под `K`,
конверт DNS под `M`, отпечаток, добивка до ступени; шифрованный снимок базы
(`rep.bin`, финальная реплика) под `K`; обе стороны.

Двоичная запись пакета (формат 2):

    4  метка «AWGK»          1  версия формата (2)
    4  поколение пакета      4  поколение M
    2+ SecretBox_M(K): длина и шифротекст (16 + 24 + 32 байта)
    4+ конверт DNS: длина и SecretBox_M(zlib(JSON)); 0 — конверта нет
    4  число частей под K, затем на каждую — 1+ имя, 1 вид, 4+ длина и шифротекст:
         вид J — SecretBox_K(zlib(JSON)): часть VPN, часть бота без базы, журнал;
         вид B — SecretBox_K(8 размер + zlib(байты)): двоичная часть как есть —
         реплика базы `db`, без hex и без JSON
    …  добивка нулями до ступени STEP
   32  отпечаток BLAKE2b всего выше (открыт; по нему сверяют поколение и целостность)

Снимок `rep.bin` (формат 2):

    4  метка «AWGS»   1  версия (2)   8  размер базы (открыт: по нему бот резерва
                                         ставит потолок памяти гостя, ключа у него нет)
    …  SecretBox_K(4+ zlib(JSON метаданных) + 8 размер + zlib(база))

Размер внутри шифротекста сверяется с открытым. Двоичная часть открывается в
`Packed`: расшифрована, но сжата, размер известен до распаковки, распаковка —
потоком в файл (`Packed.write_to`) — процесс открытия на резерве не держит базу
в памяти ни разу целиком; распаковка больше заявленного размера (или
MAX_DB) — отказ. Прежнего формата (1: база hex внутри JSON части бота) нет —
ни сборки, ни чтения: пары создаются только между серверами одного формата.

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
import io
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
SNAP_MAGIC = b"AWGS"
FORMAT = 2
STEP = 64 * 1024
MAX_SIZE = 16 * 1024 * 1024
MAX_DB = 4096 * 1024 * 1024      # двоичная часть больше — заведомо мусор (тот же верх, что у хранилища гостя)
K_LEN = 32
_DIGEST = 32
_PERSON = b"awgbot-rsvpkg-v1"
KIND_JSON = b"J"
KIND_BYTES = b"B"
DB_PART = "db"                   # реплика базы — двоичной частью
_IN_STEP = 64 * 1024             # распаковка: вход ступенями…
_OUT_STEP = 1024 * 1024          # …и выход не больше мегабайта за шаг — ни полной копии, ни бомбы в памяти


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


def _unbox(key: bytes, data, what: str) -> bytes:
    """Расшифровать срез (bytes или memoryview пакета: копия — только этого среза)."""
    try:
        return bytes(SecretBox(key).decrypt(bytes(data)))
    except (CryptoError, ValueError) as e:
        raise PackageError(f"{what}: не тот ключ или порча") from e


def _pack_json(obj) -> bytes:
    return zlib.compress(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode(), 6)


def _unpack_json(raw, what: str):
    try:
        return json.loads(zlib.decompress(raw).decode())
    except (zlib.error, ValueError, UnicodeDecodeError) as e:
        raise PackageError(f"{what}: не разобралось") from e


def _pack_bytes(data: bytes) -> bytes:
    return struct.pack(">Q", len(data)) + zlib.compress(data, 6)


class Packed:
    """Двоичная часть, уже расшифрованная, но сжатая: размер (`size`) известен до
    распаковки, распаковка — потоком (`write_to`), полной копии в памяти нет;
    `read` — байты целиком (основа, слияние, тесты)."""

    __slots__ = ("size", "_z", "_what")

    def __init__(self, plain, what: str):
        mv = memoryview(plain)
        if len(mv) < 8:
            raise PackageError(f"{what}: оборвана")
        (size,) = struct.unpack(">Q", mv[:8])
        if size > MAX_DB:
            raise PackageError(f"{what}: больше предела")
        self.size, self._z, self._what = int(size), mv[8:], what

    def write_to(self, f) -> int:
        """Распаковать в файл (что угодно с `write`); больше или меньше заявленного
        размера, порча — PackageError. Возвращает число записанных байт."""
        d = zlib.decompressobj()
        n = 0

        def put(out: bytes) -> None:
            nonlocal n
            n += len(out)
            if n > self.size:
                raise PackageError(f"{self._what}: больше заявленного размера")
            if out:
                f.write(out)
        try:
            for i in range(0, len(self._z), _IN_STEP):
                data = self._z[i:i + _IN_STEP]
                while data:
                    put(d.decompress(data, _OUT_STEP))
                    data = d.unconsumed_tail
            put(d.flush())
        except zlib.error as e:
            raise PackageError(f"{self._what}: не разобралось") from e
        if not d.eof or d.unused_data or n != self.size:
            raise PackageError(f"{self._what}: оборвана или с хвостом")
        return n

    def read(self) -> bytes:
        buf = io.BytesIO()
        self.write_to(buf)
        return buf.getvalue()


def seal(parts: dict[str, object], *, k: bytes, m: bytes, pkg_gen: int, m_gen: int,
         dns: dict | None = None, blobs: dict[str, bytes] | None = None) -> bytes:
    """Собрать пакет. `parts` — имя → объект JSON (часть VPN, часть бота…), `blobs` —
    имя → двоичные данные (реплика базы `db`)."""
    if len(k) != K_LEN:
        raise PackageError(f"ключ пакета — {K_LEN} байт")
    items = [(n, KIND_JSON, _pack_json(o)) for n, o in parts.items()]
    items += [(n, KIND_BYTES, _pack_bytes(bytes(b))) for n, b in (blobs or {}).items()]
    if len({n for n, _k, _p in items}) != len(items):
        raise PackageError("имя части повторяется")
    mkey = rsvkeys.box_key(m)
    out = [MAGIC, struct.pack(">BII", FORMAT, int(pkg_gen), int(m_gen))]
    ek = _box(mkey, k)
    out += [struct.pack(">H", len(ek)), ek]
    env = _box(mkey, _pack_json(dns)) if dns else b""
    out += [struct.pack(">I", len(env)), env, struct.pack(">I", len(items))]
    for name, kind, plain in items:
        nm = str(name).encode("ascii")
        if not 1 <= len(nm) <= 255:
            raise PackageError("имя части — 1…255 знаков ASCII")
        ct = _box(k, plain)
        out += [struct.pack(">B", len(nm)), nm, kind, struct.pack(">I", len(ct)), ct]
    body = b"".join(out)
    del out
    total = len(body) + _DIGEST
    pad = (-total) % STEP
    if total + pad > MAX_SIZE:
        raise PackageError("пакет больше предела 16 МБ")
    body += b"\x00" * pad
    return body + hashlib.blake2b(body, digest_size=_DIGEST, person=_PERSON).digest()


def _check(blob: bytes) -> memoryview:
    """Тело пакета (без отпечатка) — срезом памяти пакета, без копии."""
    if len(blob) < 4 + 9 + 2 + 4 + 4 + _DIGEST or len(blob) > MAX_SIZE:
        raise PackageError("пакет не того размера")
    mv = memoryview(blob)
    body, got = mv[:-_DIGEST], bytes(mv[-_DIGEST:])
    if hashlib.blake2b(body, digest_size=_DIGEST, person=_PERSON).digest() != got:
        raise PackageError("отпечаток пакета не сошёлся")
    if bytes(body[:4]) != MAGIC or body[4] != FORMAT:
        raise PackageError("это не пакет переезда или другой формат")
    return body


def _walk(body: memoryview):
    """(pkg_gen, m_gen, ek, env, [(имя, вид, шифротекст)…]) — срезы тела, без копий."""
    try:
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
            name = bytes(body[p:p + nl]).decode("ascii", errors="replace"); p += nl
            kind = bytes(body[p:p + 1]); p += 1
            (ln,) = struct.unpack(">I", body[p:p + 4]); p += 4
            parts.append((name, kind, body[p:p + ln])); p += ln
    except (struct.error, IndexError) as e:
        raise PackageError("пакет оборван") from e
    if p > len(body) or any(kind not in (KIND_JSON, KIND_BYTES) for _n, kind, _c in parts):
        raise PackageError("пакет оборван или с частью неизвестного вида")
    return pkg_gen, m_gen, ek, env, parts


def header(blob: bytes) -> Header:
    """Открытая часть: поколения, размер, отпечаток, есть ли конверт DNS."""
    pkg_gen, m_gen, _ek, env, _parts = _walk(_check(blob))
    return Header(pkg_gen=pkg_gen, m_gen=m_gen, size=len(blob), digest=bytes(blob[-_DIGEST:]).hex(), has_dns=bool(len(env)))


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
    if not len(env):
        return None
    data = _unpack_json(_unbox(rsvkeys.box_key(m), env, "конверт DNS"), "конверт DNS")
    if not isinstance(data, dict):
        raise PackageError("конверт DNS не того вида")
    return data


def open_parts(blob: bytes, k: bytes) -> dict[str, object]:
    """Части-JSON пакета — ключом `K` (двоичные — `open_blob`)."""
    _pkg, _mg, _ek, _env, parts = _walk(_check(blob))
    out = {}
    for name, kind, ct in parts:
        if kind == KIND_JSON:
            out[name] = _unpack_json(_unbox(k, ct, f"часть {name}"), f"часть {name}")
    return out


def open_blob(blob: bytes, k: bytes, name: str = DB_PART) -> Packed:
    """Двоичная часть `name` — ключом `K`: расшифрована, распаковка — потоком
    (`Packed.write_to`). Нет такой части — PackageError."""
    _pkg, _mg, _ek, _env, parts = _walk(_check(blob))
    for nm, kind, ct in parts:
        if nm == name and kind == KIND_BYTES:
            return Packed(_unbox(k, ct, f"часть {name}"), f"часть {name}")
    raise PackageError(f"в пакете нет части {name}")


def digest(blob: bytes) -> str:
    return bytes(blob[-_DIGEST:]).hex() if len(blob) >= _DIGEST else ""


_SNAP_HEAD = 4 + 1 + 8


def snapshot_seal(k: bytes, meta: dict, db: bytes) -> bytes:
    """Шифрованный снимок базы (`rep.bin`, финальная реплика при передаче бота) — под `K`:
    метаданные (`pkg_gen`, `at`) JSON-ом, база — двоичной частью; размер базы — открыт."""
    if not isinstance(meta, dict):
        raise PackageError("метаданные снимка — объект")
    mz = _pack_json(meta)
    plain = struct.pack(">I", len(mz)) + mz + _pack_bytes(bytes(db))
    return SNAP_MAGIC + struct.pack(">BQ", FORMAT, len(db)) + _box(k, plain)


def snapshot_db_size(blob: bytes) -> int:
    """Открытый размер базы снимка (без ключа); 0 — не снимок этого формата."""
    if len(blob) < _SNAP_HEAD or bytes(blob[:4]) != SNAP_MAGIC or blob[4] != FORMAT:
        return 0
    (n,) = struct.unpack(">Q", bytes(blob[5:_SNAP_HEAD]))
    return int(n) if n <= MAX_DB else 0


def snapshot_open(k: bytes, blob: bytes) -> tuple[dict, Packed]:
    """(метаданные, база) снимка — ключом `K`; база — `Packed` (распаковка потоком)."""
    if len(blob) < _SNAP_HEAD or bytes(blob[:4]) != SNAP_MAGIC or blob[4] != FORMAT:
        raise PackageError("снимок: не снимок или другой формат")
    plain = _unbox(k, memoryview(blob)[_SNAP_HEAD:], "снимок")
    try:
        (ln,) = struct.unpack(">I", plain[:4])
    except struct.error as e:
        raise PackageError("снимок оборван") from e
    meta = _unpack_json(memoryview(plain)[4:4 + ln], "снимок")
    if not isinstance(meta, dict):
        raise PackageError("снимок не того вида")
    db = Packed(memoryview(plain)[4 + ln:], "снимок")
    if db.size != snapshot_db_size(blob):
        raise PackageError("снимок: размер базы не сошёлся с заголовком")
    return meta, db
