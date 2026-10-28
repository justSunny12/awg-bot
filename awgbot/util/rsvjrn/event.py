"""
event.py — часть пакета rsvjrn (см. __init__): запечатка записей журнала под
`K` — событий держателя и пачек счётчиков.

Запись — открытый заголовок и шифротекст:

    2  метка «AJ»            1  вид: E — событие, a / b — пачка счётчиков стороны
    8  линия (db_line)       8  эпоха (у пачки — 0)
    8  номер (событие — i, пачка — номер пачки хоста)
    4  длина шифротекста     …  SecretBox_K(JSON, добитый пробелами)

Заголовок видит резерв без `K`: по нему он раскладывает записи по сегментам,
проверяет порядок номеров и снимает подтверждённое снимком. Внутри — те же
линия, эпоха и номер: открывающий сверяет их с заголовком, так что резерв не
может незаметно переставить, повторить или перенести запись в другую линию.
Шифротекст добивается до ступени 256 байт — длина не выдаёт, что поменяли.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

from .apply import Event
from .counts import Batch

MAGIC = b"AJ"
KIND_EVENT = b"E"
SIDES = ("a", "b")                     # a — основа, b — клон на резерве (бот основы на резерве)
STEP = 256
HEAD = struct.Struct(">2sc8sQQI")      # метка, вид, линия, эпоха, номер, длина
MAX_RECORD = 4 * 1024 * 1024           # одна запись — с запасом под пачку счётчиков сотен устройств
VERSION = 1


class RecordError(ValueError):
    """Запись не разобралась: порча, чужой ключ, чужая линия, заголовок не сошёлся."""


@dataclass(frozen=True)
class Head:
    kind: str            # "E" | "a" | "b"
    line: str            # hex
    e: int
    n: int               # номер события или последний номер пачки
    size: int            # длина всей записи (заголовок + шифротекст)


def _line_raw(line: str) -> bytes:
    try:
        raw = bytes.fromhex(str(line or ""))
    except ValueError as e:
        raise RecordError("линия не hex") from e
    if len(raw) != 8:
        raise RecordError("линия не 8 байт")
    return raw


def _pad(body: bytes) -> bytes:
    """Добивка пробелами: шифротекст (тело + 40 байт SecretBox) — кратный STEP."""
    need = (-(len(body) + SecretBox.NONCE_SIZE + SecretBox.MACBYTES)) % STEP
    return body + b" " * need


def _seal(k: bytes, kind: bytes, line: str, e: int, n: int, inner: dict) -> bytes:
    body = _pad(json.dumps(inner, ensure_ascii=False, separators=(",", ":")).encode())
    ct = bytes(SecretBox(k).encrypt(body))
    if len(ct) > MAX_RECORD:
        raise RecordError("запись больше предела")
    return HEAD.pack(MAGIC, kind, _line_raw(line), int(e), int(n), len(ct)) + ct


def head(raw: bytes, offset: int = 0) -> Head:
    """Открытый заголовок записи (без ключа)."""
    if len(raw) - offset < HEAD.size:
        raise RecordError("запись оборвана")
    magic, kind, line, e, n, ln = HEAD.unpack_from(raw, offset)
    if magic != MAGIC:
        raise RecordError("это не запись журнала")
    k = kind.decode("ascii", errors="replace")
    if k != "E" and k not in SIDES:
        raise RecordError("вид записи неизвестен")
    if ln > MAX_RECORD:
        raise RecordError("запись больше предела")
    return Head(kind=k, line=line.hex(), e=int(e), n=int(n), size=HEAD.size + int(ln))


def _open(k: bytes, raw: bytes) -> tuple[Head, dict]:
    h = head(raw)
    if len(raw) != h.size:
        raise RecordError("длина записи не сошлась с заголовком")
    try:
        body = bytes(SecretBox(k).decrypt(raw[HEAD.size:]))
        inner = json.loads(body.decode())
    except (CryptoError, ValueError, UnicodeDecodeError) as e:
        raise RecordError("запись не открылась — не тот ключ или порча") from e
    if not isinstance(inner, dict) or int(inner.get("v") or 0) != VERSION:
        raise RecordError("запись другой версии")
    if str(inner.get("line") or "") != h.line:
        raise RecordError("линия внутри записи не та, что в заголовке")
    return h, inner


def seal_event(k: bytes, line: str, ev: Event) -> bytes:
    inner = {"v": VERSION, "line": line, "e": ev.e, "i": ev.i, "t": ev.t, "op": ev.op, "k": ev.k,
             "c": ev.c, "o": ev.o, "at": ev.at}
    return _seal(k, KIND_EVENT, line, ev.e, ev.i, inner)


def open_event(k: bytes, raw: bytes, line: str = "") -> Event:
    """Событие из записи; линия (если задана), эпоха и номер внутри = заголовку — иначе RecordError."""
    h, inner = _open(k, raw)
    if h.kind != "E":
        raise RecordError("запись не событие")
    if line and h.line != line:
        raise RecordError("событие чужой линии")
    if int(inner.get("e", -1)) != h.e or int(inner.get("i", -1)) != h.n:
        raise RecordError("эпоха или номер внутри события не те, что в заголовке")
    if not isinstance(inner.get("k"), dict):
        raise RecordError("ключ строки в событии не того вида")
    return Event(i=h.n, e=h.e, t=str(inner.get("t") or ""), op=str(inner.get("op") or ""), k=inner["k"],
                 c=inner.get("c") if isinstance(inner.get("c"), dict) else None,
                 o=inner.get("o") if isinstance(inner.get("o"), dict) else None, at=str(inner.get("at") or ""))


def seal_counts(k: bytes, line: str, side: str, batch: Batch) -> bytes:
    if side not in SIDES:
        raise RecordError("сторона пачки — a или b")
    inner = {**batch.as_json(), "line": line, "side": side}
    return _seal(k, side.encode(), line, 0, batch.bn, inner)


def open_counts(k: bytes, raw: bytes, line: str = "") -> tuple[str, Batch]:
    """(сторона, пачка) из записи; номер и сторона внутри = заголовку — иначе RecordError."""
    h, inner = _open(k, raw)
    if h.kind not in SIDES:
        raise RecordError("запись не пачка счётчиков")
    if line and h.line != line:
        raise RecordError("пачка чужой линии")
    if str(inner.get("side") or "") != h.kind or int(inner.get("bn", -1)) != h.n:
        raise RecordError("сторона или номер внутри пачки не те, что в заголовке")
    try:
        return h.kind, Batch.from_json(inner)
    except (KeyError, TypeError, ValueError) as e:
        raise RecordError("пачка не того вида") from e


def split(blob: bytes) -> list[bytes]:
    """Записи подряд (тело `jev`/`jcnt`, сегмент) → по одной; обрыв или мусор — RecordError."""
    out, p = [], 0
    while p < len(blob):
        h = head(blob, p)
        if p + h.size > len(blob):
            raise RecordError("запись оборвана")
        out.append(blob[p:p + h.size])
        p += h.size
    return out
